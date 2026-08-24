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
same ``(rule_id, target_date)`` pair — the executor ran, the
operator will see the outcome in the ``All attempts`` table, and
re-listing it as "pending" would be misleading. The key is the
operator-local calendar day rather than the slot instant because a
single-day override can move the class time, so the instant the
executor recorded need not be the instant the rule projects
(ADR-0012, Decision 2).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.models import BookingDayOverride, BookingOutcome, SchedulerRule, VacationWindow
from ..scheduler.rule_jobs import (
    next_window_open_for_rule,
    target_slot_for_window,
)
from .overrides import effective_slot_for, load_overrides_in_range, local_date_for_slot

SlotKind = Literal["granted", "pending", "vacation", "modified"]


@dataclass(frozen=True)
class UpcomingSlot:
    """One upcoming attendance in the operator's local calendar.

    ``target_class`` and ``target_slot`` always carry the *effective*
    target, so callers that only want to render the class need no
    override awareness. The ``rule_*`` fields carry what the rule would
    have booked, so a ``modified`` row can annotate what was replaced
    without a second query (FR-020).
    """

    kind: SlotKind
    target_slot: datetime  # timezone-aware UTC, effective
    target_class: str  # effective
    rule_id: int | None  # None only for orphaned outcomes (rare)
    booking_id: int | None  # None when kind != "granted"
    fallback_index: int | None  # only set on granted with a second shot
    target_date: date | None = None  # operator-local day, the override key
    rule_class_type: str | None = None
    rule_class_time: str | None = None
    override_id: int | None = None
    # None when no override applies, so "not validated" and "nothing to
    # validate" stay distinguishable.
    validated: bool | None = None


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
    overrides = load_overrides_in_range(
        session,
        gym_account_id=gym_account_id,
        start=local_date_for_slot(_now),
        end=local_date_for_slot(horizon),
    )
    pending: list[UpcomingSlot] = _project_pending(
        session,
        gym_account_id=gym_account_id,
        now=_now,
        horizon=horizon,
        covered_keys=covered_keys,
        vacation_ranges=vacation_ranges,
        overrides=overrides,
        max_per_rule=max_per_rule,
    )

    granted_slots: list[UpcomingSlot] = list(granted_by_key.values())
    combined = granted_slots + pending
    # Sorting on ``target_slot`` sorts on the effective time, so a day
    # moved to a later hour reorders correctly (FR-021).
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
            target_date=local_date_for_slot(row.target_slot),
        )
    return index


def _load_covered_keys(
    session: Session,
    gym_account_id: int,
    now: datetime,
    horizon: datetime,
) -> set[tuple[int | None, date]]:
    """Return every ``(rule_id, target_date)`` an outcome already covers.

    Any terminal status counts as "the executor ran for this day"
    — pending projection must not double-list. Keyed on the
    operator-local day rather than the slot instant so an outcome
    recorded at an override's time still suppresses the rule's own
    projection for that day.
    """
    rows = session.execute(
        select(BookingOutcome.rule_id, BookingOutcome.target_slot).where(
            BookingOutcome.gym_account_id == gym_account_id,
            BookingOutcome.target_slot >= now,
            BookingOutcome.target_slot <= horizon,
        )
    ).all()
    return {(rule_id, local_date_for_slot(target_slot)) for rule_id, target_slot in rows}


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
    covered_keys: set[tuple[int | None, date]],
    vacation_ranges: list[tuple[datetime, datetime]],
    overrides: dict[tuple[int, date], BookingDayOverride],
    max_per_rule: int,
) -> list[UpcomingSlot]:
    """Project each active rule's next occurrences and drop covered ones.

    A projection whose ``target_slot`` falls inside an open vacation
    window is emitted with ``kind="vacation"`` so the history page can
    show that the class will be skipped, rather than a misleading
    "scheduled" chip. Vacation is evaluated against the effective slot
    and still wins over any override (FR-029).
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
                rule_slot = target_slot_for_window(rule, window_open)
            except ValueError:
                # Malformed HH:MM — operator-data bug. Skip this rule
                # so the projection still returns the others.
                break
            if rule_slot > horizon:
                break
            target_date = local_date_for_slot(rule_slot)
            if (int(rule.id), target_date) in covered_keys:
                # Executor already ran for this day; the granted row
                # (or a non-granted terminal) already tells that
                # story. Advance the cursor and keep projecting.
                cursor = window_open + timedelta(seconds=1)
                continue

            override = overrides.get((int(rule.id), target_date))
            replaces_target = override is not None and (
                override.class_type is not None or override.class_time is not None
            )
            target_slot = rule_slot
            target_class = str(rule.class_type)
            if override is not None and replaces_target:
                target_class = override.class_type or str(rule.class_type)
                if override.class_time is not None:
                    target_slot = effective_slot_for(rule, target_date, override.class_time)

            on_vacation = any(start <= target_slot <= end for start, end in vacation_ranges)
            if on_vacation:
                kind: SlotKind = "vacation"
            elif replaces_target:
                kind = "modified"
            else:
                kind = "pending"
            projections.append(
                UpcomingSlot(
                    kind=kind,
                    target_slot=target_slot,
                    target_class=target_class,
                    rule_id=int(rule.id),
                    booking_id=None,
                    fallback_index=None,
                    target_date=target_date,
                    rule_class_type=str(rule.class_type),
                    rule_class_time=str(rule.class_time),
                    override_id=int(override.id) if override is not None else None,
                    validated=bool(override.validated) if override is not None else None,
                )
            )
            cursor = window_open + timedelta(seconds=1)
    return projections


__all__ = [
    "SlotKind",
    "UpcomingSlot",
    "list_upcoming_slots",
]
