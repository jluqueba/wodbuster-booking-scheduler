"""User/account queries (ADR-0010)."""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .models import OperatorProfile

# Sentinel for an indefinite (reversible) ban: any real ban date is before it,
# so ``ban_is_active`` stays true until the admin clears ``banned_until``.
INDEFINITE_BAN = datetime(9999, 12, 31, tzinfo=UTC)


def ban_is_active(banned_until: datetime | None, now: datetime) -> bool:
    """Return whether a ban is currently in force."""
    return banned_until is not None and banned_until > now


def count_pending_signups(session: Session) -> int:
    """Return how many signups are awaiting admin approval."""
    total = session.scalar(
        select(func.count(OperatorProfile.id)).where(OperatorProfile.status == "pending")
    )
    return int(total or 0)


__all__ = ["INDEFINITE_BAN", "ban_is_active", "count_pending_signups"]
