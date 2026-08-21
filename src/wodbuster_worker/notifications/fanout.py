"""Fan-out of email outbox rows next to Telegram rows (ADR-0011).

Producers (booking outcomes, cancellation, cookie/heartbeat alerts) already
enqueue a banner + a Telegram row per event. This helper adds the email row
for the same event when the recipient opted in: they have an email address and
the relevant per-type preference is on. The dispatcher re-checks the same
predicate as defense in depth, but gating here keeps disabled categories out of
the queue entirely.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy.orm import Session

from ..persistence.models import NotificationOutbox, OperatorProfile

# Outbox payload ``kind`` -> toggleable email preference category. Kinds absent
# here (e.g. transactional signup mail) are always sent.
EMAIL_CATEGORY: dict[str, str] = {
    "booking_result": "bookings",
    "cookie_expiring": "session_alerts",
    "cookie_invalid": "session_alerts",
    "heartbeat_anomaly": "session_alerts",
}


def email_allowed(operator: OperatorProfile | None, payload: dict[str, Any]) -> bool:
    """Whether ``operator`` should receive an email for this payload."""
    if operator is None or not operator.email:
        return False
    category = EMAIL_CATEGORY.get(str(payload.get("kind")))
    if category is None:
        return True
    prefs = operator.email_preferences or {}
    return bool(prefs.get(category, True))


def enqueue_email_row(
    session: Session,
    *,
    operator: OperatorProfile | None,
    gym_account_id: int,
    payload: dict[str, Any],
    now: datetime,
) -> None:
    """Enqueue an email outbox row when the operator opted in."""
    if not email_allowed(operator, payload):
        return
    assert operator is not None  # narrowed by email_allowed
    session.add(
        NotificationOutbox(
            user_id=operator.id,
            gym_account_id=gym_account_id,
            kind="email",
            target=operator.email or "",
            payload=payload,
            enqueued_at=now,
        )
    )


__all__ = ["EMAIL_CATEGORY", "email_allowed", "enqueue_email_row"]
