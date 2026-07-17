"""Cookie-expiring alert evaluator (US4.3, US4.4).

Runs after every heartbeat probe. Compares the freshly-projected TTL
against the next scheduled booking window and decides one of four
actions:

- :class:`Emit` — either the first emission of an alert, or a
  re-emission because the underlying condition still holds and the
  operator has not acknowledged since the previous cycle.
- :class:`Suppress` — an open alert exists, the condition still holds,
  but the operator acknowledged this alert since the last heartbeat.
  US4.3 grants one grace cycle before re-nagging.
- :class:`Clear` — the condition no longer holds and an open alert
  exists. Close it so a subsequent successful window does not fire a
  stale banner. Also invoked from :meth:`CookieStore.save`'s
  clear-on-refresh path (US4.4).
- :class:`NoOp` — nothing to do (no open alert, no threshold breach).

The evaluator is split into a pure decision function
(:func:`evaluate_cookie_expiring`) and an imperative applier
(:func:`apply_alert_action`) so the decision logic can be unit-tested
without touching Postgres.

Payload contract for the alert row and the outbox rows:

``payload = {
    "kind": "cookie_expiring",
    "next_window_at": <iso datetime, UTC>,
    "projected_ttl_at": <iso datetime, UTC>,
}``

The outbox dispatcher (US2.1, out of scope here) reads these fields to
render the Telegram message and the banner text.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import (
    Alert,
    HeartbeatReading,
    NotificationOutbox,
    OperatorProfile,
)
from .next_window import compute_next_window

_ALERT_KIND = "cookie_expiring"
_DEFAULT_LEAD_TIME = timedelta(hours=24)


@dataclass(frozen=True)
class Emit:
    """Alert should be emitted or re-emitted; write outbox rows."""

    next_window_at: datetime
    projected_ttl_at: datetime


@dataclass(frozen=True)
class Suppress:
    """Skip this cycle — operator acknowledged the open alert since last tick."""


@dataclass(frozen=True)
class Clear:
    """Close the currently-open alert; condition no longer holds."""


@dataclass(frozen=True)
class NoOp:
    """Nothing to do."""


AlertAction = Emit | Suppress | Clear | NoOp


def evaluate_cookie_expiring(
    *,
    session: Session,
    operator_id: int,
    projected_ttl_at: datetime | None,
    now: datetime,
    lead_time: timedelta = _DEFAULT_LEAD_TIME,
    previous_heartbeat_at: datetime | None = None,
) -> AlertAction:
    """Decide what to do with the ``cookie_expiring`` alert this cycle.

    Reads from the DB (open alert, next window) but does not mutate.
    Callers pass the outcome of :meth:`HeartbeatProbe.run` and get back
    a plain dataclass they can hand to :func:`apply_alert_action`.
    """
    if projected_ttl_at is None:
        # No projection yet (freshly pasted, no Valid probe seen). No
        # data → no alert. Clear any historical open alert defensively.
        if _open_alert(session, operator_id) is not None:
            return Clear()
        return NoOp()

    next_window_at = compute_next_window(session, operator_id, now)
    if next_window_at is None:
        # No scheduled window in view. If an alert somehow remained
        # open (say a rule was deleted), close it.
        if _open_alert(session, operator_id) is not None:
            return Clear()
        return NoOp()

    # Threshold: cookie will not survive the next window AND the window
    # is close enough that a 24h lead time matters. Both must hold —
    # a far-future window is not an emergency, even if the cookie is
    # projected to expire before it.
    cookie_dies_before_window = projected_ttl_at < next_window_at
    within_lead_time = next_window_at - now <= lead_time
    threshold_holds = cookie_dies_before_window and within_lead_time

    open_alert = _open_alert(session, operator_id)
    if not threshold_holds:
        return Clear() if open_alert is not None else NoOp()

    if open_alert is None:
        return Emit(
            next_window_at=next_window_at,
            projected_ttl_at=projected_ttl_at,
        )

    # An open alert already exists. Suppress if the operator
    # acknowledged it since the previous heartbeat. ``acknowledged_at``
    # persists across cycles; the "since previous heartbeat" comparison
    # is what makes suppression a one-cycle grace, not permanent.
    if (
        open_alert.acknowledged_at is not None
        and previous_heartbeat_at is not None
        and open_alert.acknowledged_at >= previous_heartbeat_at
    ):
        return Suppress()

    return Emit(
        next_window_at=next_window_at,
        projected_ttl_at=projected_ttl_at,
    )


def apply_alert_action(
    session: Session,
    operator_id: int,
    action: AlertAction,
    *,
    now: datetime,
) -> int | None:
    """Materialise ``action`` in the DB. Returns the alert row's id (if any).

    The caller (usually :func:`run_heartbeat_tick`) owns the
    transaction. Both :class:`Emit` writes and :class:`Clear` closures
    stay inside the same transaction as the heartbeat write so the
    audit trail lines up: one commit per (probe, alert) pair.

    On :class:`Emit`, two ``notification_outbox`` rows are appended
    (kind = ``telegram`` and ``banner``). The Telegram row is skipped
    when the operator has no ``telegram_chat_id`` on file — US-007
    binds that later, and pushing a row with an empty target would
    just crash the dispatcher.
    """
    if isinstance(action, NoOp | Suppress):
        return None

    if isinstance(action, Clear):
        open_alert = _open_alert(session, operator_id)
        if open_alert is not None:
            open_alert.closed_at = now
            return int(open_alert.id)
        return None

    # Emit: get-or-create the open alert row, refresh last_emitted_at,
    # write the two outbox rows.
    open_alert = _open_alert(session, operator_id)
    payload = {
        "kind": _ALERT_KIND,
        "next_window_at": action.next_window_at.isoformat(),
        "projected_ttl_at": action.projected_ttl_at.isoformat(),
    }
    if open_alert is None:
        alert = Alert(
            operator_id=operator_id,
            kind=_ALERT_KIND,
            payload=payload,
            first_emitted_at=now,
            last_emitted_at=now,
        )
        session.add(alert)
        session.flush()  # populate alert.id for the outbox rows below
    else:
        alert = open_alert
        alert.payload = payload
        alert.last_emitted_at = now

    _enqueue_outbox_rows(session, operator_id, alert.id, payload, now=now)
    return int(alert.id)


def close_open_cookie_expiring(session: Session, operator_id: int, *, now: datetime) -> int | None:
    """Close the operator's open ``cookie_expiring`` alert, if any.

    Called from :meth:`CookieStore.save` for the clear-on-refresh
    contract (US4.4): a successful re-paste means the operator has
    dealt with the underlying condition; the alert should stop nagging
    immediately, not on the next heartbeat.
    """
    open_alert = _open_alert(session, operator_id)
    if open_alert is None:
        return None
    open_alert.closed_at = now
    return int(open_alert.id)


def acknowledge_open_cookie_expiring(
    session: Session, operator_id: int, *, now: datetime
) -> int | None:
    """Acknowledge the operator's open ``cookie_expiring`` alert (US4/FR-027).

    Powers the Telegram ``/ack`` command. Sets ``acknowledged_at`` on
    the single open ``cookie_expiring`` row for ``operator_id`` so the
    evaluator suppresses re-emission for the current heartbeat cycle
    (see :func:`evaluate_cookie_expiring`). The underlying condition is
    not cleared — acknowledgement only quiets the nag for one cycle.

    Returns the acknowledged alert id, or ``None`` when the operator
    has no open ``cookie_expiring`` alert (nothing to acknowledge).
    The ``_open_alert`` filter is scoped to ``operator_id`` so one
    operator can never acknowledge another's alert (FR-005).
    """
    open_alert = _open_alert(session, operator_id)
    if open_alert is None:
        return None
    open_alert.acknowledged_at = now
    return int(open_alert.id)


def _open_alert(session: Session, operator_id: int) -> Alert | None:
    """Return the currently-open ``cookie_expiring`` row, or ``None``."""
    return session.scalar(
        select(Alert)
        .where(
            Alert.operator_id == operator_id,
            Alert.kind == _ALERT_KIND,
            Alert.closed_at.is_(None),
        )
        .limit(1)
    )


def previous_heartbeat_at(
    session: Session, operator_id: int, current_probed_at: datetime
) -> datetime | None:
    """Return the ``probed_at`` of the heartbeat that preceded ``current``.

    Uses a strict less-than so a call made after the current row has
    been written still sees the true previous row (there is no
    equality collision unless two probes share a microsecond, which
    would already indicate a scheduler bug).
    """
    return session.scalar(
        select(HeartbeatReading.probed_at)
        .where(
            HeartbeatReading.operator_id == operator_id,
            HeartbeatReading.probed_at < current_probed_at,
        )
        .order_by(HeartbeatReading.probed_at.desc())
        .limit(1)
    )


def _enqueue_outbox_rows(
    session: Session,
    operator_id: int,
    alert_id: int,
    payload: dict[str, str],
    *,
    now: datetime,
) -> None:
    """Append one outbox row per channel (Telegram, banner)."""
    outbox_payload = {**payload, "alert_id": alert_id}

    # Banner row: target is the operator id as a string; the dashboard
    # partial filters by operator anyway, but every outbox row needs a
    # non-null target for the dispatcher.
    session.add(
        NotificationOutbox(
            operator_id=operator_id,
            kind="banner",
            target=str(operator_id),
            payload=outbox_payload,
            enqueued_at=now,
        )
    )

    # Telegram: only when the operator has bound a chat id. US-007
    # populates that; before then, silently skip so the dispatcher
    # never sees an empty-target row.
    operator = session.get(OperatorProfile, operator_id)
    if operator is None or not operator.telegram_chat_id:
        return
    session.add(
        NotificationOutbox(
            operator_id=operator_id,
            kind="telegram",
            target=operator.telegram_chat_id,
            payload=outbox_payload,
            enqueued_at=now,
        )
    )


__all__ = [
    "AlertAction",
    "Clear",
    "Emit",
    "NoOp",
    "Suppress",
    "apply_alert_action",
    "close_open_cookie_expiring",
    "evaluate_cookie_expiring",
    "previous_heartbeat_at",
]
