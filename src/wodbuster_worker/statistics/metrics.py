"""Metric computation over captured attendance rows (ADR-0015).

Every function here is pure: it takes already-loaded rows and the
current instant, and returns a value object. None of them opens a
session, reads the clock, or touches a template. That is what makes
each rule in ADR-0015 testable as the sentence it implements, and it is
why no test in this area needs to freeze a global.

Three rules are enforced once, at the boundary, rather than repeated in
every function:

- a class is invisible to the metric layer until its settle window has
  elapsed (INV-003), because the upstream state is still changeable by a
  coach while the class is running;
- a removal recorded at or after the class start is reclassified out of
  the voluntary cancellation set (INV-010), because at a gym with the
  attendance controls disabled that is what a coach marking an absence
  looks like;
- a removal that is really a class change is reclassified too, because
  on a day that holds a training a removal is a move from one hour to
  another, and the abandonment rate is meant to answer "I booked and
  ended up not training".
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal

from ..persistence.models import AttendanceRecord

# What the metric layer sees, after the reclassifications above. The
# stored vocabulary has three values; the extra two are derived rather
# than persisted, so a rule can change without a migration.
CountedState = Literal["attended", "cancelled", "no_show", "removed_after_start", "swapped"]

# What one calendar cell says about a day, in priority order. A class
# change wins over a training because it is the rarer fact and the
# training is still visible as the class name in the cell.
DayStatus = Literal[
    "attended",
    "swapped",
    "cancelled",
    "removed_after_start",
    "no_show",
    "no_activity",
    "closed",
    "uncaptured",
    "future",
]


@dataclass(frozen=True)
class CountedRecord:
    """One attendance record, settled and classified."""

    local_date: date
    start_at: datetime
    state: CountedState
    class_name: str
    class_type_id: int | None
    capacity: int | None
    occupancy: int
    ever_full: bool
    state_changed_at: datetime | None

    @property
    def cancellation_lead(self) -> timedelta | None:
        """How far before the class the operator removed themselves.

        ``None`` when the record is not a voluntary cancellation or when
        the upstream instant was missing. Never negative: a removal at
        or after the start is a different state entirely.
        """
        if self.state != "cancelled" or self.state_changed_at is None:
            return None
        return self.start_at - self.state_changed_at


def settle_cutoff(now: datetime, *, settle_window_hours: float) -> datetime:
    """Return the latest class start instant that already counts."""
    return now - timedelta(hours=settle_window_hours)


def counted_records(
    records: Iterable[AttendanceRecord],
    *,
    now: datetime,
    settle_window_hours: float,
) -> list[CountedRecord]:
    """Filter and classify raw rows for the metric layer.

    This is the only place the boundary rules live. Every metric
    function below consumes the result, so no rule can be forgotten in
    one metric and applied in another.
    """
    cutoff = settle_cutoff(now, settle_window_hours=settle_window_hours)
    settled = [record for record in records if record.start_at <= cutoff]
    swapped_days = _swapped_days(settled)

    counted: list[CountedRecord] = []
    for record in settled:
        counted.append(
            CountedRecord(
                local_date=record.local_date,
                start_at=record.start_at,
                state=_classify(record, swapped_days=swapped_days),
                class_name=record.class_name,
                class_type_id=record.class_type_id,
                capacity=record.capacity,
                occupancy=record.occupancy,
                ever_full=record.ever_full,
                state_changed_at=record.state_changed_at,
            )
        )
    return counted


def _swapped_days(records: Sequence[AttendanceRecord]) -> set[date]:
    """Return the days on which a removal was really a class change.

    The rule is the owner's definition of what the abandonment rate
    measures: "I booked and ended up not training". On a day that holds
    a training, a removal is a move from one hour to another, not a
    drop-out.

    An earlier version matched the identical upstream state instant,
    which is the signature WodBuster's own ``Calendario_Mover`` handler
    leaves. It was strictly more precise and answered the wrong
    question: it counted a class released two days in advance as an
    abandonment on a day the user trained.

    Counting either kind as an abandonment roughly doubled the rate on
    the first month of real data, in the single figure the feature
    exists to report.
    """
    return {record.local_date for record in records if record.state == "attended"}


@dataclass(frozen=True)
class DayCell:
    """One day of the calendar, with everything a cell needs to render.

    ``status`` is the colour the cell is painted with, but it is not
    the whole truth about the day: a day can hold a training and a
    removal at once, and a cell has one background. The counts and
    ``attended_names`` are what lets the template list every outcome as
    its own line, which is what makes the headline figures reconcilable
    with the grid. They count classes; the colour describes a day.
    """

    day: date
    status: DayStatus
    attended: int
    cancelled: int
    swapped: int
    removed_after_start: int
    no_show: int
    attended_names: tuple[str, ...]


@dataclass(frozen=True)
class Calendar:
    """The selected range as calendar weeks, Monday first."""

    weeks: tuple[tuple[DayCell | None, ...], ...]
    attended: int
    cancelled: int
    swapped: int


def day_calendar(
    records: Sequence[CountedRecord],
    *,
    captured: Mapping[date, int],
    start: date,
    end: date,
    today: date,
) -> Calendar:
    """Build the calendar for ``start`` to ``end`` inclusive.

    ``captured`` maps a local date to how many classes the gym ran that
    day, taken from the capture ledger. It is what separates the cases a
    bar chart cannot tell apart: the gym was closed, the gym was open
    and nothing was booked, and the day has never been read. Treating
    the third as the second is what INV-005 forbids.

    ``today`` separates a fourth case that only appears once the
    calendar shows whole months: a day that has not happened yet is not
    a day we failed to read.

    Weeks are padded with ``None`` so the grid always has seven columns
    and the weekday headers mean what they say.
    """
    by_day: dict[date, list[CountedRecord]] = {}
    for record in records:
        by_day.setdefault(record.local_date, []).append(record)

    cells: list[DayCell] = []
    cursor = start
    while cursor <= end:
        cells.append(_cell_for(cursor, by_day.get(cursor, []), captured, today))
        cursor += timedelta(days=1)

    weeks: list[tuple[DayCell | None, ...]] = []
    if cells:
        row: list[DayCell | None] = [None] * cells[0].day.weekday()
        for cell in cells:
            row.append(cell)
            if len(row) == 7:
                weeks.append(tuple(row))
                row = []
        if row:
            weeks.append(tuple(row + [None] * (7 - len(row))))

    return Calendar(
        weeks=tuple(weeks),
        attended=sum(cell.attended for cell in cells),
        cancelled=sum(cell.cancelled for cell in cells),
        swapped=sum(cell.swapped for cell in cells),
    )


def _cell_for(
    day: date,
    records: Sequence[CountedRecord],
    captured: Mapping[date, int],
    today: date,
) -> DayCell:
    counts = {
        "attended": sum(1 for r in records if r.state == "attended"),
        "cancelled": sum(1 for r in records if r.state == "cancelled"),
        "swapped": sum(1 for r in records if r.state == "swapped"),
        "removed_after_start": sum(1 for r in records if r.state == "removed_after_start"),
        "no_show": sum(1 for r in records if r.state == "no_show"),
    }
    class_count = captured.get(day)

    status: DayStatus
    if counts["swapped"]:
        # A class change wins the cell colour even though the day also
        # holds a training: it is the rarer, more informative fact, and
        # the reader can see the training in the class name below it.
        status = "swapped"
    elif counts["attended"]:
        status = "attended"
    elif counts["cancelled"]:
        status = "cancelled"
    elif counts["removed_after_start"]:
        status = "removed_after_start"
    elif counts["no_show"]:
        status = "no_show"
    elif day > today:
        status = "future"
    elif class_count is None:
        status = "uncaptured"
    elif class_count == 0:
        status = "closed"
    else:
        status = "no_activity"

    return DayCell(
        day=day,
        status=status,
        attended=counts["attended"],
        cancelled=counts["cancelled"],
        swapped=counts["swapped"],
        removed_after_start=counts["removed_after_start"],
        no_show=counts["no_show"],
        attended_names=tuple(dict.fromkeys(r.class_name for r in records if r.state == "attended")),
    )


def _classify(record: AttendanceRecord, *, swapped_days: set[date]) -> CountedState:
    """Apply the reclassification rules to one stored row."""
    if record.state != "cancelled":
        # ``state`` is constrained by the database enum, so the three
        # stored values are the only ones that reach here.
        return "attended" if record.state == "attended" else "no_show"
    changed = record.state_changed_at
    if changed is not None and changed >= record.start_at:
        # Checked before the class-change rule on purpose: being removed
        # once the class had started is an absence, and training
        # something else later the same day does not turn it into a move.
        return "removed_after_start"
    if record.local_date in swapped_days:
        return "swapped"
    return "cancelled"


__all__ = [
    "Calendar",
    "CountedRecord",
    "CountedState",
    "DayCell",
    "DayStatus",
    "counted_records",
    "day_calendar",
    "settle_cutoff",
]
