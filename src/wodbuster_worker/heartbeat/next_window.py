"""Next-window lookahead for the operator's active scheduler rules (US4.2).

Given ``now``, returns the datetime at which the earliest upcoming
booking window opens for the operator. That instant is what the alert
evaluator compares the projected cookie TTL against.

Delegates the per-rule arithmetic to
:func:`scheduler.rule_jobs.next_window_open_for_rule` so the heartbeat
evaluator and the booking scheduler agree byte-for-byte on when a
window opens.

The function returns ``None`` when the operator has no eligible rule.
The alert evaluator interprets ``None`` as "no window in view → no
alert".
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import SchedulerRule
from ..scheduler.rule_jobs import next_window_open_for_rule


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
            window_open = next_window_open_for_rule(rule, now=now)
        except ValueError:
            # A malformed HH:MM is an operator-data bug; skip and let
            # the next-window computation move on.
            continue
        candidates.append(window_open)

    if not candidates:
        return None
    return min(candidates)


__all__ = ["compute_next_window"]
