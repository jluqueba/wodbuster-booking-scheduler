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

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..booking.vacation import find_covering_window
from ..persistence.models import SchedulerRule
from ..scheduler.rule_jobs import (
    next_window_open_for_rule,
    target_slot_for_window,
)

# Upper bound on how many weekly windows to roll through while skipping
# vacation-covered targets before giving up on a rule. Weekly rules
# repeat every 7 days, so 60 covers well over a year — far past any
# realistic vacation range. Prevents an unbounded loop if every target
# in view happens to be covered.
_MAX_VACATION_ROLL = 60


def compute_next_window(session: Session, gym_account_id: int, now: datetime) -> datetime | None:
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
                SchedulerRule.gym_account_id == gym_account_id,
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


@dataclass(frozen=True)
class NextBooking:
    """Bundle of "the next thing the scheduler will do".

    ``window_open`` is when the booking window opens (also what
    :func:`compute_next_window` returns). ``target_slot`` is the
    class-start datetime the winning rule is going to book. Both
    are timezone-aware UTC. ``rule_id`` lets the caller cross-
    reference the row that produced this projection.
    """

    window_open: datetime
    target_slot: datetime
    rule_id: int


def compute_next_booking(
    session: Session, gym_account_id: int, now: datetime
) -> NextBooking | None:
    """Return richer info about the next scheduled booking.

    Same selection semantics as :func:`compute_next_window` (earliest
    upcoming window across active rules) but also returns the class
    slot the rule is aiming at, and — unlike :func:`compute_next_window`
    — SKIPS windows whose target class falls inside an open vacation
    window. The booking executor skips those targets at run time
    (``booking.executor`` US7.2 skip guard via
    :func:`booking.vacation.find_covering_window`), so a countdown to a
    booking that will be skipped is misleading. For each rule this rolls
    forward week by week to the first target that is not vacation-
    covered; a rule whose every in-view target is covered contributes
    nothing. Dashboard/Telegram surface only; the alert evaluator sticks
    to the leaner :func:`compute_next_window`.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")

    rules = (
        session.execute(
            select(SchedulerRule).where(
                SchedulerRule.gym_account_id == gym_account_id,
                SchedulerRule.active.is_(True),
            )
        )
        .scalars()
        .all()
    )

    best: NextBooking | None = None
    for rule in rules:
        projection = _next_bookable_window(session, rule, gym_account_id, now)
        if projection is None:
            continue
        window_open, target_slot = projection
        if best is None or window_open < best.window_open:
            best = NextBooking(
                window_open=window_open,
                target_slot=target_slot,
                rule_id=int(rule.id),
            )
    return best


def _next_bookable_window(
    session: Session,
    rule: SchedulerRule,
    gym_account_id: int,
    now: datetime,
) -> tuple[datetime, datetime] | None:
    """Earliest ``(window_open, target_slot)`` for ``rule`` whose target
    is not covered by an open vacation window.

    Rolls forward one week at a time from ``now``, skipping any target
    the booking executor would skip, bounded by ``_MAX_VACATION_ROLL``.
    Returns ``None`` when the rule has malformed timing data or when
    every target within the lookahead horizon is vacation-covered.
    """
    cursor = now
    for _ in range(_MAX_VACATION_ROLL):
        try:
            window_open = next_window_open_for_rule(rule, now=cursor)
            target_slot = target_slot_for_window(rule, window_open)
        except ValueError:
            # A malformed HH:MM is an operator-data bug; skip the rule.
            return None
        covering = find_covering_window(
            session,
            gym_account_id=gym_account_id,
            target_slot=target_slot,
            now=now,
        )
        if covering is None:
            return window_open, target_slot
        # Target sits inside a vacation window; the executor would skip
        # it. Roll to the following week's window (passing the current
        # window as ``now`` makes ``next_window_open_for_rule`` advance
        # exactly seven days).
        cursor = window_open
    return None


__all__ = ["NextBooking", "compute_next_booking", "compute_next_window"]
