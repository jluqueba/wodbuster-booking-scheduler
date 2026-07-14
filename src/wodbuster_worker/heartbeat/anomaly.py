"""Per-run anomaly detector (US2.4, FR-026, CC-008).

Every 60 seconds the scheduler ticks :func:`run_anomaly_tick`. The
tick asks :func:`detect_missed_windows` which active rules had their
booking window open in the recent past *without* a paired
``booking_outcome`` row landing in the database. Each missed
``(rule, window)`` pair is a "silent run": the executor never
touched WodBuster, or it did but the outcome writer failed to
commit, or the scheduler itself stalled and the tick never
happened.

The alert is aggregated per operator via the "one open alert per
(operator, kind)" pattern shared with :mod:`heartbeat.alerts`: if
an open ``heartbeat_anomaly`` already exists we refresh
``last_emitted_at`` instead of inserting a duplicate. Callers own
the transaction; both the alert row and the paired outbox rows are
written inside the same session so the plan's cross-cutting rule
holds (an operator never sees a Telegram burst for an alert that
failed to persist and vice versa).

Grace period bounds "recent": a window that opened less than the
grace ago is still considered "in flight" — the retry loop inside
:class:`BookingExecutor.book` can take up to a couple of minutes,
so we do not want to raise a false anomaly just because the tick
fired between the window open and the outcome commit. Default 5
minutes; expose as a knob so tests can shrink it.

Lookback bounds the historical scan window: we only inspect
occurrences in ``(now - lookback, now - grace_period]``. Anything
older than ``lookback`` is water under the bridge; alerting on
week-old missed runs would just add noise. Default 60 minutes,
which is a comfortable buffer for a 60-second tick + a scheduler
that briefly missed its beat.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import (
    Alert,
    BookingOutcome,
    NotificationOutbox,
    OperatorProfile,
    SchedulerRule,
)
from ..scheduler.rule_jobs import (
    next_window_open_for_rule,
    target_slot_for_window,
)

_log = structlog.get_logger(__name__)

_ALERT_KIND = "heartbeat_anomaly"

DEFAULT_GRACE_PERIOD = timedelta(minutes=5)
DEFAULT_LOOKBACK = timedelta(minutes=60)


@dataclass(frozen=True)
class MissedWindow:
    """One rule/window pair that fired without producing an outcome."""

    rule_id: int
    operator_id: int
    target_class: str
    window_open: datetime
    target_slot: datetime


def detect_missed_windows(
    session: Session,
    *,
    now: datetime,
    grace_period: timedelta = DEFAULT_GRACE_PERIOD,
    lookback: timedelta = DEFAULT_LOOKBACK,
) -> list[MissedWindow]:
    """Return the missed ``(rule, window)`` pairs across all active rules.

    A pair is "missed" when:

    - the rule is active,
    - the rule's most-recent past window opened at least
      ``grace_period`` ago (so a currently-running attempt is not
      flagged prematurely),
    - the window opened no more than ``lookback`` ago (older gaps
      are considered water under the bridge),
    - the rule already existed when the window opened
      (``rule.created_at <= window_open``), and
    - no ``booking_outcome`` row references that ``(rule_id,
      target_slot)`` pair.

    The last check treats *any* terminal status as "the executor
    ran": ``granted``, ``full``, ``skipped`` (vacation),
    ``cookie_invalid`` and the rest all count as evidence that the
    tick happened. Missing means "no row at all".
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    cutoff_recent = now - grace_period
    cutoff_ancient = now - lookback

    rules = (
        session.execute(select(SchedulerRule).where(SchedulerRule.active.is_(True))).scalars().all()
    )

    missed: list[MissedWindow] = []
    for rule in rules:
        try:
            next_open = next_window_open_for_rule(rule, now=now)
        except ValueError:
            # Malformed HH:MM is an operator-data bug; skip and let
            # someone fix the rule.
            continue

        # Every rule runs weekly, so the previous instance is exactly
        # 7 days before the next one.
        last_open = next_open - timedelta(days=7)

        if last_open >= cutoff_recent:
            # Still inside the grace window (or in the future).
            continue
        if last_open <= cutoff_ancient:
            # Older than the lookback horizon.
            continue
        if rule.created_at is not None and rule.created_at > last_open:
            # Rule did not exist at the time the window would have
            # opened — nothing to have missed.
            continue

        try:
            target_slot = target_slot_for_window(rule, last_open)
        except ValueError:
            continue

        if _outcome_exists(session, rule_id=int(rule.id), target_slot=target_slot):
            continue

        missed.append(
            MissedWindow(
                rule_id=int(rule.id),
                operator_id=int(rule.operator_id),
                target_class=str(rule.class_type),
                window_open=last_open,
                target_slot=target_slot,
            )
        )

    return missed


def emit_anomaly_alerts(
    session: Session,
    missed: Iterable[MissedWindow],
    *,
    now: datetime,
) -> list[int]:
    """Get-or-create one ``heartbeat_anomaly`` alert per operator.

    Groups ``missed`` by ``operator_id``. For each group, upserts the
    open alert row, refreshes ``last_emitted_at``, replaces
    ``payload`` with the current set of missed windows, and enqueues
    banner + Telegram outbox rows. Returns the alert ids that were
    touched (created or refreshed) so tests can assert cardinality.
    """
    grouped: dict[int, list[MissedWindow]] = {}
    for m in missed:
        grouped.setdefault(m.operator_id, []).append(m)

    touched: list[int] = []
    for operator_id, windows in grouped.items():
        payload = _build_payload(windows)
        alert = _open_alert(session, operator_id)
        if alert is None:
            alert = Alert(
                operator_id=operator_id,
                kind=_ALERT_KIND,
                payload=payload,
                first_emitted_at=now,
                last_emitted_at=now,
            )
            session.add(alert)
            session.flush()
        else:
            alert.payload = payload
            alert.last_emitted_at = now

        _enqueue_outbox_rows(
            session,
            operator_id=operator_id,
            alert_id=int(alert.id),
            payload=payload,
            now=now,
        )
        touched.append(int(alert.id))

    return touched


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _outcome_exists(session: Session, *, rule_id: int, target_slot: datetime) -> bool:
    """Return True when at least one ``booking_outcome`` row matches."""
    hit = session.execute(
        select(BookingOutcome.id)
        .where(
            BookingOutcome.rule_id == rule_id,
            BookingOutcome.target_slot == target_slot,
        )
        .limit(1)
    ).scalar_one_or_none()
    return hit is not None


def _open_alert(session: Session, operator_id: int) -> Alert | None:
    return session.scalar(
        select(Alert)
        .where(
            Alert.operator_id == operator_id,
            Alert.kind == _ALERT_KIND,
            Alert.closed_at.is_(None),
        )
        .limit(1)
    )


def _build_payload(windows: list[MissedWindow]) -> dict[str, object]:
    return {
        "kind": _ALERT_KIND,
        "text": _render_text(windows),
        "missed": [
            {
                "rule_id": w.rule_id,
                "target_class": w.target_class,
                "window_open": w.window_open.astimezone(UTC).isoformat(),
                "target_slot": w.target_slot.astimezone(UTC).isoformat(),
            }
            for w in windows
        ],
    }


def _render_text(windows: list[MissedWindow]) -> str:
    """Operator-facing summary of the missed windows."""
    if len(windows) == 1:
        w = windows[0]
        return (
            "Heartbeat anomaly: no booking outcome recorded for "
            f"{w.target_class} at {w.target_slot.astimezone(UTC):%a %d %b %H:%M UTC}. "
            "Check the worker logs."
        )
    return (
        "Heartbeat anomaly: "
        f"{len(windows)} scheduled bookings did not produce an "
        "outcome. Check the worker logs."
    )


def _enqueue_outbox_rows(
    session: Session,
    *,
    operator_id: int,
    alert_id: int,
    payload: dict[str, object],
    now: datetime,
) -> None:
    outbox_payload = {**payload, "alert_id": alert_id}

    session.add(
        NotificationOutbox(
            operator_id=operator_id,
            kind="banner",
            target=str(operator_id),
            payload=outbox_payload,
            enqueued_at=now,
        )
    )

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
    "DEFAULT_GRACE_PERIOD",
    "DEFAULT_LOOKBACK",
    "MissedWindow",
    "detect_missed_windows",
    "emit_anomaly_alerts",
]
