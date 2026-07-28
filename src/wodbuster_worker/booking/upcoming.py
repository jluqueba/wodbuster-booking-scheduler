"""Upcoming-attendance projection for the history page (H.1 full+).

The plain ``list_upcoming_bookings`` helper only surfaces
``booking_outcome`` rows that are already ``granted`` — i.e. classes
WodBuster has already confirmed. That answers "what am I attending?"
but misses the operator's other question: "what is my scheduler
going to attempt next?".

This module fills that gap. :func:`list_upcoming_slots` merges two
sources into one chronological list of :class:`UpcomingSlot`:

1. **granted** — real ``booking_outcome`` rows whose ``target_slot``
   sits in ``[now, now + horizon]``.
2. **pending** — active ``SchedulerRule`` projections whose next
   ``target_slot`` sits in the same window, *and* for which no
   matching outcome exists yet.

The pending case relies on ``next_window_open_for_rule`` and
``target_slot_for_window`` from :mod:`scheduler.rule_jobs`, so the
day/time arithmetic honours ``WORKER_TIMEZONE`` and stays byte-for-
byte consistent with what the scheduler will actually fire.

Terminal statuses other than ``granted`` (``skipped``, ``cancelled``,
``full``, ``cookie_invalid``, ...) suppress the pending slot for the
same ``(rule_id, target_slot)`` pair — the executor ran, the
operator will see the outcome in the ``All attempts`` table, and
re-listing it as "pending" would be misleading.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import BookingOutcome, SchedulerRule, VacationWindow
from ..scheduler.rule_jobs import (
    next_window_open_for_rule,
    target_slot_for_window,
)

SlotKind = Literal["granted", "pending", "vacation"]


@dataclass(frozen=True)
class UpcomingSlot:
    """One upcoming attendance in the operator's local calendar."""

    kind: SlotKind
    target_slot: datetime  # timezone-aware UTC
    target_class: str
    rule_id: int | None  # None only for orphaned outcomes (rare)
    booking_id: int | None  # None when kind != "granted"
    fallback_index: int | None  # only set on granted with a second shot


def list_upcoming_slots(
    session: Session,
    gym_account_id: int,
    *,
    now: datetime | None = None,
    horizon_days: int = 14,
    max_per_rule: int = 5,
) -> list[UpcomingSlot]:
    """Return granted + pending attendance in chronological order.

    ``horizon_days`` caps the projection window. ``max_per_rule``
    guards against a runaway loop should ``next_window_open_for_rule``
    somehow stop advancing (defensive; the arithmetic is
    deterministic).
    """
    _now = now if now is not None else datetime.now(tz=UTC)
    horizon = _now + timedelta(days=horizon_days)

    granted_by_key = _load_granted_index(session, gym_account_id, _now, horizon)
    covered_keys = _load_covered_keys(session, gym_account_id, _now, horizon)
    vacation_ranges = _load_open_vacation_ranges(session, gym_account_id, _now, horizon)
    pending: list[UpcomingSlot] = _project_pending(
        session,
        gym_account_id=gym_account_id,
        now=_now,
        horizon=horizon,
        covered_keys=covered_keys,
        vacation_ranges=vacation_ranges,
        max_per_rule=max_per_rule,
    )

    granted_slots: list[UpcomingSlot] = list(granted_by_key.values())
    combined = granted_slots + pending
    combined.sort(key=lambda s: s.target_slot)
    return combined


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _load_granted_index(
    session: Session,
    gym_account_id: int,
    now: datetime,
    horizon: datetime,
) -> dict[tuple[int | None, datetime], UpcomingSlot]:
    """Return granted outcomes in ``[now, horizon]`` keyed by rule+slot."""
    rows = session.execute(
        select(BookingOutcome)
        .where(
            BookingOutcome.gym_account_id == gym_account_id,
            BookingOutcome.terminal_status == "granted",
            BookingOutcome.target_slot >= now,
            BookingOutcome.target_slot <= horizon,
        )
        .order_by(BookingOutcome.target_slot.asc())
    ).scalars()
    index: dict[tuple[int | None, datetime], UpcomingSlot] = {}
    for row in rows:
        key = (row.rule_id, row.target_slot)
        index[key] = UpcomingSlot(
            kind="granted",
            target_slot=row.target_slot,
            target_class=str(row.target_class),
            rule_id=row.rule_id,
            booking_id=int(row.id),
            fallback_index=row.granted_fallback_index,
        )
    return index


def _load_covered_keys(
    session: Session,
    gym_account_id: int,
    now: datetime,
    horizon: datetime,
) -> set[tuple[int | None, datetime]]:
    """Return every ``(rule_id, target_slot)`` an outcome already covers.

    Any terminal status counts as "the executor ran for this slot"
    — pending projection must not double-list.
    """
    rows = session.execute(
        select(BookingOutcome.rule_id, BookingOutcome.target_slot).where(
            BookingOutcome.gym_account_id == gym_account_id,
            BookingOutcome.target_slot >= now,
            BookingOutcome.target_slot <= horizon,
        )
    ).all()
    return {(rule_id, target_slot) for rule_id, target_slot in rows}


def _load_open_vacation_ranges(
    session: Session,
    gym_account_id: int,
    now: datetime,
    horizon: datetime,
) -> list[tuple[datetime, datetime]]:
    """Return ``(start, end)`` for each open vacation window in range.

    "Open" mirrors the scheduler skip-guard
    (:func:`vacation.find_covering_window`): ``closed_at IS NULL`` and
    ``end_date >= now``. Windows that start after the projection
    horizon cannot cover any upcoming slot, so they are filtered out.
    The ranges are matched in memory against each projected slot so
    the projection issues one query instead of one per slot.
    """
    rows = session.execute(
        select(VacationWindow.start_date, VacationWindow.end_date).where(
            VacationWindow.gym_account_id == gym_account_id,
            VacationWindow.closed_at.is_(None),
            VacationWindow.end_date >= now,
            VacationWindow.start_date <= horizon,
        )
    ).all()
    return [(start, end) for start, end in rows]


def _project_pending(
    session: Session,
    *,
    gym_account_id: int,
    now: datetime,
    horizon: datetime,
    covered_keys: set[tuple[int | None, datetime]],
    vacation_ranges: list[tuple[datetime, datetime]],
    max_per_rule: int,
) -> list[UpcomingSlot]:
    """Project each active rule's next occurrences and drop covered ones.

    A projection whose ``target_slot`` falls inside an open vacation
    window is emitted with ``kind="vacation"`` so the history page can
    show that the class will be skipped, rather than a misleading
    "scheduled" chip.
    """
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
    projections: list[UpcomingSlot] = []
    for rule in rules:
        cursor = now
        for _ in range(max_per_rule):
            try:
                window_open = next_window_open_for_rule(rule, now=cursor)
                target_slot = target_slot_for_window(rule, window_open)
            except ValueError:
                # Malformed HH:MM — operator-data bug. Skip this rule
                # so the projection still returns the others.
                break
            if target_slot > horizon:
                break
            if (int(rule.id), target_slot) in covered_keys:
                # Executor already ran for this slot; the granted row
                # (or a non-granted terminal) already tells that
                # story. Advance the cursor and keep projecting.
                cursor = window_open + timedelta(seconds=1)
                continue
            on_vacation = any(start <= target_slot <= end for start, end in vacation_ranges)
            projections.append(
                UpcomingSlot(
                    kind="vacation" if on_vacation else "pending",
                    target_slot=target_slot,
                    target_class=str(rule.class_type),
                    rule_id=int(rule.id),
                    booking_id=None,
                    fallback_index=None,
                )
            )
            cursor = window_open + timedelta(seconds=1)
    return projections


__all__ = [
    "SlotKind",
    "UpcomingSlot",
    "list_upcoming_slots",
]
