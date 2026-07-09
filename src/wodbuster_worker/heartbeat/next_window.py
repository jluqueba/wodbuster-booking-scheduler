"""Next-window lookahead for the operator's active scheduler rules (US4.2).

Given ``now``, returns the datetime at which the earliest upcoming
booking window opens for the operator. That instant is what the alert
evaluator compares the projected cookie TTL against.

Semantics (rule model v2):

- A ``scheduler_rule`` sets ``day_of_week`` (0=Mon..6=Sun) as the
  *attendance* day, and ``booking_opens_days_before`` /
  ``booking_opens_at`` as the number of days earlier and the clock
  time at which WodBuster opens the reservation window.
- The rule fires on ``trigger_day = (day_of_week -
  booking_opens_days_before) mod 7`` at ``booking_opens_at``.
- Inactive rules are skipped.

The function returns ``None`` when the operator has no eligible rule.
The alert evaluator interprets ``None`` as "no window in view → no
alert".

All arithmetic runs in UTC. The rule table does not carry a timezone
column today (single tenant, single locale); when it does, the
conversion happens here.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import SchedulerRule


def compute_next_window(
    session: Session, operator_id: int, now: datetime
) -> datetime | None:
    """Return the earliest upcoming booking-window datetime.

    The value is timezone-aware UTC. Callers compare it against
    ``projected_ttl_at`` (also UTC) and ``now``.
    """
    if now.tzinfo is None:
        # Refuse naive datetimes rather than silently assuming UTC —
        # the ambiguity has bitten schedulers before.
        raise ValueError("now must be timezone-aware")

    rules = (
        session.execute(
            select(SchedulerRule).where(
                SchedulerRule.operator_id == operator_id,
                SchedulerRule.active.is_(True),
            )
        )
        .scalars()
        .all()
    )

    candidates: list[datetime] = []
    for rule in rules:
        try:
            opens_at = _parse_time_slot(rule.booking_opens_at)
        except ValueError:
            # A malformed time is an operator-data bug; skip and let
            # the next-window computation move on.
            continue

        trigger_day = (rule.day_of_week - rule.booking_opens_days_before) % 7
        window_open = _next_occurrence(now=now, day_of_week=trigger_day, at=opens_at)
        candidates.append(window_open)

    if not candidates:
        return None
    return min(candidates)


def _parse_time_slot(value: str) -> time:
    """Parse ``HH:MM`` into :class:`~datetime.time`."""
    hh, mm = value.split(":")
    return time(hour=int(hh), minute=int(mm))


def _next_occurrence(*, now: datetime, day_of_week: int, at: time) -> datetime:
    """Return the next occurrence of ``day_of_week`` at ``at``.

    If today matches ``day_of_week`` and the time is still in the
    future, the same-day instant is returned. Otherwise it rolls
    forward to the next matching weekday.
    """
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_ahead = (day_of_week - now.weekday()) % 7
    candidate = (today + timedelta(days=days_ahead)).replace(
        hour=at.hour, minute=at.minute
    ).astimezone(UTC)
    if candidate <= now:
        candidate += timedelta(days=7)
    return candidate


__all__ = ["compute_next_window"]
