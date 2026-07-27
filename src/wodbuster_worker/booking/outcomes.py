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
a persistent banner alongside the one-shot Telegram burst.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

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


def persist_outcome(
    session: Session,
    *,
    gym_account_id: int,
    rule_id: int | None,
    target_class: str,
    target_slot: datetime,
    terminal_status: str,
    granted_fallback_index: int | None = None,
    response_payload: str | None = None,
    telegram_text: str,
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

    For ``terminal_status == "cookie_invalid"`` an ``Alert`` row is
    opened (or refreshed if one is already open) so the banner
    surfaces the persistent condition.
    """
    _now = now or datetime.now(tz=UTC)

    outcome = BookingOutcome(
        gym_account_id=gym_account_id,
        rule_id=rule_id,
        target_class=target_class,
        target_slot=target_slot,
        attempted_at=_now,
        terminal_status=terminal_status,
        granted_fallback_index=granted_fallback_index,
        response_payload=response_payload,
    )
    session.add(outcome)
    session.flush()  # populate outcome.id for outbox payload

    _enqueue_outbox_rows(
        session,
        gym_account_id=gym_account_id,
        outcome_id=int(outcome.id),
        terminal_status=terminal_status,
        text=telegram_text,
        now=_now,
    )

    if terminal_status == "cookie_invalid":
        _open_or_refresh_cookie_invalid_alert(session, gym_account_id=gym_account_id, now=_now)

    return outcome


def _enqueue_outbox_rows(
    session: Session,
    *,
    gym_account_id: int,
    outcome_id: int,
    terminal_status: str,
    text: str,
    now: datetime,
) -> None:
    """Append one banner + one Telegram outbox row for the outcome.

    Outbox delivery is user-scoped (ADR-0007): rows carry the owning
    ``user_id`` plus the ``gym_account_id`` for context. The Telegram
    row is skipped when the user has not registered a chat id (US-007
    wires that later); a row with an empty target would only churn the
    dispatcher until it exhausted retries.
    """
    payload: dict[str, Any] = {
        "kind": "booking_result",
        "terminal_status": terminal_status,
        "outcome_id": outcome_id,
        "text": text,
    }

    gym_account = session.get(GymAccount, gym_account_id)
    if gym_account is None:  # pragma: no cover - FK guarantees presence
        return
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
    if operator is None or not operator.telegram_chat_id:
        return
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


def _open_or_refresh_cookie_invalid_alert(
    session: Session, *, gym_account_id: int, now: datetime
) -> None:
    """Insert or update the gym account's open ``cookie_invalid`` alert.

    The partial unique index on ``alert`` (open per gym_account+kind)
    means we cannot naively insert; look up the existing row first.
    """
    existing = session.scalar(
        select(Alert).where(
            Alert.gym_account_id == gym_account_id,
            Alert.kind == _COOKIE_INVALID_ALERT_KIND,
            Alert.closed_at.is_(None),
        )
    )
    if existing is not None:
        existing.last_emitted_at = now
        return
    session.add(
        Alert(
            gym_account_id=gym_account_id,
            kind=_COOKIE_INVALID_ALERT_KIND,
            payload={"kind": _COOKIE_INVALID_ALERT_KIND},
            first_emitted_at=now,
            last_emitted_at=now,
        )
    )


__all__ = ["persist_outcome"]
