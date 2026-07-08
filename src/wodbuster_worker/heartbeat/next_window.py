"""Next-window lookahead for the operator's active scheduler rules (US4.2).

Given ``now``, returns the datetime at which the earliest upcoming
booking window opens for the operator. That instant is what the alert
evaluator compares the projected cookie TTL against.

Semantics:

- A ``scheduler_rule`` sets ``day_of_week`` (0=Mon..6=Sun) and
  ``window_offset_hours`` (how many hours before class start the
  booking window opens).
- Class start time is derived from the rule's first
  :class:`ClassPreference` (``target_time_slot``, ``HH:MM``). Multiple
  preferences are fallbacks against a single time slot; if that
  assumption changes we'll thread the multi-slot case through here.
- Inactive rules are skipped. Rules without any preferences are also
  skipped (no time slot → no derivable window).

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

from ..persistence.models import ClassPreference, SchedulerRule


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

    # Load every active rule for the operator together with its first
    # preference (order_index=0). We only need the preference's time
    # slot; a rule without any preference is unschedulable and gets
    # filtered out.
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
        first_pref = _first_preference(session, rule.id)
        if first_pref is None:
            continue
        try:
            time_slot = _parse_time_slot(first_pref.target_time_slot)
        except ValueError:
            # A malformed time slot is an operator-data bug; the alert
            # evaluator is the wrong place to surface it, so skip and
            # let the next-window computation move on.
            continue

        window_open = _next_window_for_rule(
            now=now,
            day_of_week=rule.day_of_week,
            time_slot=time_slot,
            offset_hours=rule.window_offset_hours,
        )
        candidates.append(window_open)

    if not candidates:
        return None
    return min(candidates)


def _first_preference(session: Session, rule_id: int) -> ClassPreference | None:
    """Return the ``order_index=0`` preference for the rule, if any."""
    return session.scalar(
        select(ClassPreference)
        .where(ClassPreference.rule_id == rule_id)
        .order_by(ClassPreference.order_index)
        .limit(1)
    )


def _parse_time_slot(value: str) -> time:
    """Parse ``HH:MM`` into :class:`~datetime.time`."""
    hh, mm = value.split(":")
    return time(hour=int(hh), minute=int(mm))


def _next_window_for_rule(
    *,
    now: datetime,
    day_of_week: int,
    time_slot: time,
    offset_hours: int,
) -> datetime:
    """Return the next window-open instant for a single rule.

    Class start is the next occurrence of ``day_of_week`` at ``time_slot``;
    the window opens ``offset_hours`` before that. If the calculated
    window is already in the past (window open earlier today), roll
    forward one week.
    """
    # Anchor at today's midnight in the caller's timezone.
    today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_ahead = (day_of_week - now.weekday()) % 7
    class_day = today + timedelta(days=days_ahead)
    class_start = class_day.replace(
        hour=time_slot.hour, minute=time_slot.minute
    ).astimezone(UTC)
    window_open = class_start - timedelta(hours=offset_hours)
    if window_open <= now:
        window_open += timedelta(days=7)
    return window_open


__all__ = ["compute_next_window"]
