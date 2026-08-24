"""BookingOutcome persistence (US1.8, plan cross-cutting rule).

Two responsibilities kept in one place because they share a
transaction:

1. Insert the ``booking_outcome`` row.
2. Insert the ``notification_outbox`` row(s) that carry the
   operator-visible signal for that outcome.

The plan makes the transaction contract explicit: "Every write that
mutates state and produces an operator-visible signal writes the
entity row and the corresponding ``notification_outbox`` row in the
same SQLAlchemy session-level transaction." That means a rollback of
the DB write also rolls back the notification queue — the operator
never sees a Telegram message for an outcome we failed to persist,
and never fails to see one for an outcome we did persist.

For ``cookie_invalid`` terminal outcomes we also open (or refresh) a
``cookie_invalid`` alert so the dashboard banner surfaces the
condition. That mirrors the heartbeat evaluator's contract for
``cookie_expiring`` (see ``heartbeat/alerts.py``): the operator sees
a persistent banner alongside the one-shot Telegram burst. A booking
granted through the single-day override fallback opens a
``booking_fallback`` alert the same way (ADR-0012 Decision 5), which
is what keeps INV-008 (no silent substitution) a property of the
design rather than of the operator's channel preferences.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..notifications.fanout import enqueue_email_row
from ..persistence.models import (
    Alert,
    BookingOutcome,
    GymAccount,
    NotificationOutbox,
    OperatorProfile,
)

# Alert kind reused when a booking attempt reveals the cookie has
# gone bad (cookie_invalid Res value or auth failure mid-call).
_COOKIE_INVALID_ALERT_KIND = "cookie_invalid"

# Alert kind opened when the override's class was unavailable and the
# rule's own class was booked instead (ADR-0012 Decision 5).
_BOOKING_FALLBACK_ALERT_KIND = "booking_fallback"


def persist_outcome(
    session: Session,
    *,
    gym_account_id: int,
    rule_id: int | None,
    target_class: str,
    target_slot: datetime,
    terminal_status: str,
    outcome_source: str = "rule",
    granted_fallback_index: int | None = None,
    response_payload: str | None = None,
    telegram_text: str,
    requested_class: str | None = None,
    requested_time: str | None = None,
    fallback_reason: str | None = None,
    now: datetime | None = None,
) -> BookingOutcome:
    """Persist one booking attempt and enqueue its notifications.

    Caller owns the transaction (opens the session, commits, handles
    rollback). This function only ``session.add(...)`` and
    ``session.flush()`` so the caller can compose it with other
    writes if needed.

    - ``target_slot`` is the scheduled class start time (timezone-
      aware, UTC).
    - ``response_payload`` is the raw WodBuster response body (or a
      short description on non-WodBuster terminal reasons such as
      "no cookie on file"). Persisted verbatim for post-mortem
      (FR-012).
    - ``telegram_text`` is the pre-rendered notification body. Keeping
      the copy at the writer avoids scattering the "success" /
      "failure" wording across the executor.
    - ``outcome_source`` is orthogonal to ``terminal_status``
      (ADR-0012, Decision 4): it says which plan drove the attempt
      (``rule`` or ``override``), not how it ended. It defaults to
      ``rule`` so every pre-override caller stays correct.
    - ``requested_class``, ``requested_time`` and ``fallback_reason``
      describe the override target that could not be honoured. They
      are only meaningful when ``outcome_source`` is
      ``override_fallback``, and they are what makes INV-008 hold:
      every surface can name the booked class, the requested class and
      the reason without querying the override back.

    For ``terminal_status == "cookie_invalid"`` an ``Alert`` row is
    opened (or refreshed if one is already open) so the banner
    surfaces the persistent condition. A ``granted`` outcome sourced to
    ``override_fallback`` opens a ``booking_fallback`` alert the same
    way, in this same transaction.
    """
    _now = now or datetime.now(tz=UTC)

    outcome = BookingOutcome(
        gym_account_id=gym_account_id,
        rule_id=rule_id,
        target_class=target_class,
        target_slot=target_slot,
        attempted_at=_now,
        terminal_status=terminal_status,
        outcome_source=outcome_source,
        granted_fallback_index=granted_fallback_index,
        response_payload=response_payload,
    )
    session.add(outcome)
    session.flush()  # populate outcome.id for outbox payload

    payload = _enqueue_outbox_rows(
        session,
        gym_account_id=gym_account_id,
        outcome_id=int(outcome.id),
        terminal_status=terminal_status,
        outcome_source=outcome_source,
        target_class=target_class,
        target_slot=target_slot,
        text=telegram_text,
        requested_class=requested_class,
        requested_time=requested_time,
        fallback_reason=fallback_reason,
        now=_now,
    )

    if terminal_status == "cookie_invalid":
        _open_or_refresh_alert(
            session,
            gym_account_id=gym_account_id,
            kind=_COOKIE_INVALID_ALERT_KIND,
            payload={"kind": _COOKIE_INVALID_ALERT_KIND},
            now=_now,
        )
    elif outcome_source == "override_fallback" and terminal_status == "granted":
        # The banner is not a user-toggleable channel, so this is the one
        # surface that carries the substitution whatever the operator's
        # Telegram and email preferences are (INV-008).
        _open_or_refresh_alert(
            session,
            gym_account_id=gym_account_id,
            kind=_BOOKING_FALLBACK_ALERT_KIND,
            payload={**payload, "kind": _BOOKING_FALLBACK_ALERT_KIND},
            now=_now,
        )

    return outcome


def _enqueue_outbox_rows(
    session: Session,
    *,
    gym_account_id: int,
    outcome_id: int,
    terminal_status: str,
    outcome_source: str,
    target_class: str,
    target_slot: datetime,
    text: str,
    requested_class: str | None,
    requested_time: str | None,
    fallback_reason: str | None,
    now: datetime,
) -> dict[str, Any]:
    """Append one banner + one Telegram outbox row for the outcome.

    Outbox delivery is user-scoped (ADR-0007): rows carry the owning
    ``user_id`` plus the ``gym_account_id`` for context. The Telegram
    row is skipped when the user has not registered a chat id (US-007
    wires that later); a row with an empty target would only churn the
    dispatcher until it exhausted retries.

    ``class_type`` and ``target_slot`` are stored structured so the
    dispatcher can render the Telegram body in the recipient's language
    at send time (ADR-0008); ``text`` is kept as a pre-rendered fallback.

    Returns the payload so the caller can reuse it for the alert row.
    """
    payload: dict[str, Any] = {
        "kind": "booking_result",
        "terminal_status": terminal_status,
        "outcome_source": outcome_source,
        "outcome_id": outcome_id,
        "class_type": target_class,
        "target_slot": target_slot.astimezone(UTC).isoformat(),
        "text": text,
    }
    if requested_class is not None:
        payload["requested_class"] = requested_class
    if requested_time is not None:
        payload["requested_time"] = requested_time
    if fallback_reason is not None:
        payload["fallback_reason"] = fallback_reason

    gym_account = session.get(GymAccount, gym_account_id)
    if gym_account is None:  # pragma: no cover - FK guarantees presence
        return payload
    user_id = gym_account.user_id

    session.add(
        NotificationOutbox(
            user_id=user_id,
            gym_account_id=gym_account_id,
            kind="banner",
            target=str(user_id),
            payload=payload,
            enqueued_at=now,
        )
    )

    operator = session.get(OperatorProfile, user_id)
    enqueue_email_row(
        session, operator=operator, gym_account_id=gym_account_id, payload=payload, now=now
    )
    if operator is None or not operator.telegram_chat_id:
        return payload
    session.add(
        NotificationOutbox(
            user_id=user_id,
            gym_account_id=gym_account_id,
            kind="telegram",
            target=operator.telegram_chat_id,
            payload=payload,
            enqueued_at=now,
        )
    )
    return payload


def _open_or_refresh_alert(
    session: Session,
    *,
    gym_account_id: int,
    kind: str,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    """Insert or update the gym account's open alert of ``kind``.

    The partial unique index on ``alert`` (one open row per
    gym_account + kind) means we cannot naively insert; look up the
    existing row first. A refresh overwrites the payload, so an open
    banner always describes the most recent event of that kind. For
    ``booking_fallback`` that is the accepted collapse documented in
    ADR-0012 Decision 5: the per-event detail lives on the outcome row.
    """
    existing = session.scalar(
        select(Alert).where(
            Alert.gym_account_id == gym_account_id,
            Alert.kind == kind,
            Alert.closed_at.is_(None),
        )
    )
    if existing is not None:
        existing.payload = payload
        existing.last_emitted_at = now
        return
    session.add(
        Alert(
            gym_account_id=gym_account_id,
            kind=kind,
            payload=payload,
            first_emitted_at=now,
            last_emitted_at=now,
        )
    )


__all__ = ["persist_outcome"]
