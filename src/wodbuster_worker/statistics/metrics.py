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

from collections import Counter
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal
from typing import Literal

from ..persistence.models import AttendanceRecord
from ..scheduler.clock import operator_timezone

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
    def local_start(self) -> datetime:
        """The class start in the operator's own clock.

        Stored instants are UTC, and every question the reader asks
        about time ("do I train in the evening") is a question about
        their own clock, not about UTC.
        """
        return self.start_at.astimezone(operator_timezone())

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

    @property
    def booking_lead(self) -> timedelta | None:
        """How far before the class the booking was made.

        The other end of the same interval as
        :attr:`cancellation_lead`. For an attended class the upstream
        state instant is when the booking happened, which is what the
        gym charges a point for.
        """
        if self.state != "attended" or self.state_changed_at is None:
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
class Abandonment:
    """How often a booking ended in not training.

    ``rate`` is ``None`` rather than zero when nothing was booked. A
    rate over an empty denominator is unknown, and rendering it as
    "0 percent" would report perfect behaviour to someone who has not
    been to the gym.

    The three excluded counts are carried so the page can say what the
    rate leaves out. A figure whose exclusions are invisible is a
    figure the reader cannot check.
    """

    attended: int
    cancelled: int
    swapped: int
    removed_after_start: int
    no_show: int

    @property
    def booked(self) -> int:
        """Bookings the rate is computed over."""
        return self.attended + self.cancelled

    @property
    def rate(self) -> float | None:
        if not self.booked:
            return None
        return self.cancelled / self.booked

    @property
    def percent(self) -> int | None:
        """The rate as whole percent, for display.

        Rounded half up rather than to even: a reader comparing 2 of 16
        against a rounded figure should not meet banker's rounding.
        """
        if self.rate is None:
            return None
        return int(Decimal(self.rate * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


@dataclass(frozen=True)
class CancellationBands:
    """Cancellations grouped by how much notice they gave.

    The boundaries are the gym's own penalty tiers, because those are
    the only ones with a consequence. ``unknown_lead`` is reported
    rather than folded into a band: a cancellation whose upstream
    instant was missing still happened, and hiding it would make the
    bands disagree with the abandonment count.
    """

    early: int
    late: int
    very_late: int
    unknown_lead: int
    late_hours: float
    very_late_hours: float

    @property
    def total(self) -> int:
        return self.early + self.late + self.very_late + self.unknown_lead


def abandonment(records: Sequence[CountedRecord]) -> Abandonment:
    """Return the abandonment rate and everything it excludes."""
    counts = Counter(record.state for record in records)
    return Abandonment(
        attended=counts["attended"],
        cancelled=counts["cancelled"],
        swapped=counts["swapped"],
        removed_after_start=counts["removed_after_start"],
        no_show=counts["no_show"],
    )


def cancellation_bands(
    records: Sequence[CountedRecord],
    *,
    late_hours: float,
    very_late_hours: float,
) -> CancellationBands:
    """Group voluntary cancellations by their notice period.

    A cancellation exactly on a boundary counts as the more generous
    band. The gym's own wording defines "more than four hours" and
    "less than four hours" and leaves four hours itself undefined, so
    the tie goes to the user rather than to an arbitrary choice.
    """
    early = late = very_late = unknown = 0
    late_cut = timedelta(hours=late_hours)
    very_late_cut = timedelta(hours=very_late_hours)

    for record in records:
        if record.state != "cancelled":
            continue
        lead = record.cancellation_lead
        if lead is None:
            unknown += 1
        elif lead >= late_cut:
            early += 1
        elif lead >= very_late_cut:
            late += 1
        else:
            very_late += 1

    return CancellationBands(
        early=early,
        late=late,
        very_late=very_late,
        unknown_lead=unknown,
        late_hours=late_hours,
        very_late_hours=very_late_hours,
    )


@dataclass(frozen=True)
class GridCell:
    """One weekday and class-time bucket of the training pattern."""

    weekday: int  # 0 = Monday
    slot: str  # HH:MM in the operator's clock
    attended: int


@dataclass(frozen=True)
class SlotStats:
    """What happens to the classes the operator books at one start time."""

    slot: str
    attended: int
    cancelled: int

    @property
    def booked(self) -> int:
        return self.attended + self.cancelled

    @property
    def drop_rate(self) -> float | None:
        if not self.booked:
            return None
        return self.cancelled / self.booked


@dataclass(frozen=True)
class MonthPoint:
    """One month of the attendance trend."""

    year: int
    month: int
    attended: int
    cancelled: int

    @property
    def key(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"


@dataclass(frozen=True)
class LeadBands:
    """How far ahead bookings were made.

    Separate from :class:`CancellationBands`, which measures the other
    end of the same interval. Booking early costs a point at a gym that
    charges for advance booking; cancelling late costs more.
    """

    same_day: int
    within_day: int
    early: int
    unknown: int
    free_hours: float

    @property
    def total(self) -> int:
        return self.same_day + self.within_day + self.early + self.unknown


@dataclass(frozen=True)
class Occupancy:
    """How full the classes the operator attended were.

    ``ever_full`` counts the upstream flag rather than comparing
    occupancy to capacity, because occupancy is a snapshot taken at
    capture time and the flag is the only reliable answer to "did this
    class fill up".
    """

    sessions: int
    average_fill: float | None
    ever_full: int

    @property
    def ever_full_share(self) -> float | None:
        if not self.sessions:
            return None
        return self.ever_full / self.sessions


def weekday_hour_grid(records: Sequence[CountedRecord]) -> tuple[GridCell, ...]:
    """Return attended sessions per weekday and class start time.

    Bucketed on the exact ``HH:MM`` the class starts, not on the hour
    it falls in. A gym runs classes at half past as readily as on the
    hour, and this one also runs a 10:40, so rounding to the hour
    would merge distinct slots and, worse, label a 20:30 session as
    20:00.

    Only populated buckets are returned. The template lays out the
    full grid, so an empty Sunday is a decision about presentation
    rather than a row of zeros invented here.
    """
    counts: Counter[tuple[int, str]] = Counter()
    for record in records:
        if record.state != "attended":
            continue
        local = record.local_start
        counts[(local.weekday(), local.strftime("%H:%M"))] += 1
    return tuple(
        GridCell(weekday=weekday, slot=slot, attended=count)
        for (weekday, slot), count in sorted(counts.items(), key=lambda kv: (kv[0][1], kv[0][0]))
    )


def drop_rate_by_slot(
    records: Sequence[CountedRecord], *, min_bookings: int = 1
) -> tuple[SlotStats, ...]:
    """Return the drop rate for each class start time.

    ``min_bookings`` hides slots with too little history to mean
    anything: one drop out of one booking is a 100 percent rate and a
    meaningless one. The caller picks the floor, because what counts as
    enough depends on how much history there is.
    """
    attended: Counter[str] = Counter()
    cancelled: Counter[str] = Counter()
    for record in records:
        slot = record.local_start.strftime("%H:%M")
        if record.state == "attended":
            attended[slot] += 1
        elif record.state == "cancelled":
            cancelled[slot] += 1

    slots = sorted(set(attended) | set(cancelled))
    stats = [
        SlotStats(slot=slot, attended=attended[slot], cancelled=cancelled[slot]) for slot in slots
    ]
    return tuple(stat for stat in stats if stat.booked >= min_bookings)


def monthly_trend(records: Sequence[CountedRecord]) -> tuple[MonthPoint, ...]:
    """Return attended and dropped classes per calendar month.

    Months with no activity inside the span are present with zeros: a
    month off is information, and compressing it would draw a flatter
    picture than the truth.
    """
    attended: Counter[tuple[int, int]] = Counter()
    cancelled: Counter[tuple[int, int]] = Counter()
    for record in records:
        key = (record.local_date.year, record.local_date.month)
        if record.state == "attended":
            attended[key] += 1
        elif record.state == "cancelled":
            cancelled[key] += 1

    keys = set(attended) | set(cancelled)
    if not keys:
        return ()

    first, last = min(keys), max(keys)
    points: list[MonthPoint] = []
    year, month = first
    while (year, month) <= last:
        points.append(
            MonthPoint(
                year=year,
                month=month,
                attended=attended[(year, month)],
                cancelled=cancelled[(year, month)],
            )
        )
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return tuple(points)


def booking_lead_bands(records: Sequence[CountedRecord], *, free_hours: float) -> LeadBands:
    """Group attended bookings by how far ahead they were made.

    The boundary that matters is the gym's own free window: booking
    inside it costs nothing, booking earlier costs a point. The wider
    bands above it exist to show whether the operator books days ahead
    or the night before.
    """
    free_cut = timedelta(hours=free_hours)
    day_cut = timedelta(days=1)
    same_day = within_day = early = unknown = 0

    for record in records:
        if record.state != "attended":
            continue
        lead = record.booking_lead
        if lead is None:
            unknown += 1
        elif lead < free_cut:
            same_day += 1
        elif lead < day_cut:
            within_day += 1
        else:
            early += 1

    return LeadBands(
        same_day=same_day,
        within_day=within_day,
        early=early,
        unknown=unknown,
        free_hours=free_hours,
    )


def occupancy(records: Sequence[CountedRecord]) -> Occupancy:
    """Return how full the attended classes were."""
    attended = [record for record in records if record.state == "attended"]
    fills = [
        record.occupancy / record.capacity
        for record in attended
        if record.capacity and record.capacity > 0
    ]
    return Occupancy(
        sessions=len(attended),
        average_fill=sum(fills) / len(fills) if fills else None,
        ever_full=sum(1 for record in attended if record.ever_full),
    )


@dataclass(frozen=True)
class Streak:
    """A run of training days unbroken by a day that could have held one."""

    days: int
    start: date | None
    end: date | None
    # True when the run reached the oldest captured day, so it may have
    # been longer than we can see. The page says "at least" rather than
    # claiming a number the history cannot support.
    is_lower_bound: bool = False


@dataclass(frozen=True)
class Streaks:
    """The current run and the best one in the captured history."""

    current: Streak
    longest: Streak
    # True while the backfill has not reached the horizon: a longer
    # run may exist in history nobody has read yet.
    longest_is_provisional: bool = False


def streaks(
    records: Sequence[CountedRecord],
    *,
    captured: Mapping[date, int],
    today: date,
    excluded_weekdays: Collection[int] = (),
) -> Streaks:
    """Return the current and longest training streaks.

    A day breaks a streak only when it could have held a training and
    did not: the gym ran classes, the weekday is not one the user
    excluded, and the day is over. Everything else is neutral, which is
    what lets one rule cover a gym's weekly closing day and a public
    holiday without either being configured.

    A neutral day does not count towards the streak either. Counting it
    would claim a session on a day the gym was shut.

    A day with no ledger entry ends the walk rather than breaking it.
    Absence of a reading is not absence of training (INV-005), so the
    run is reported as a lower bound instead of as a fact.
    """
    trained = {record.local_date for record in records if record.state == "attended"}
    excluded = set(excluded_weekdays)

    def breaks(day: date) -> bool:
        classes = captured.get(day)
        if classes is None or classes == 0:
            return False
        if day.weekday() in excluded:
            return False
        # A day still running cannot be a failure yet, or the streak
        # would reset every morning before the gym opens.
        return day < today

    current = _walk_back(today, trained, captured, breaks)
    longest = _best_run(trained, captured, breaks)
    if current.days > longest.days:
        longest = current

    return Streaks(
        current=current,
        longest=longest,
        longest_is_provisional=longest.is_lower_bound,
    )


def _walk_back(
    today: date,
    trained: set[date],
    captured: Mapping[date, int],
    breaks: Callable[[date], bool],
) -> Streak:
    """Return the run of training days ending at or before ``today``."""
    days: list[date] = []
    cursor = today
    hit_edge = False
    while True:
        if cursor not in captured:
            hit_edge = True
            break
        if cursor in trained:
            days.append(cursor)
        elif breaks(cursor):
            break
        cursor -= timedelta(days=1)

    if not days:
        return Streak(days=0, start=None, end=None, is_lower_bound=False)
    return Streak(
        days=len(days),
        start=days[-1],
        end=days[0],
        is_lower_bound=hit_edge,
    )


def _best_run(
    trained: set[date],
    captured: Mapping[date, int],
    breaks: Callable[[date], bool],
) -> Streak:
    """Return the longest run anywhere in the captured history.

    A run whose neighbouring day was never read is reported as a lower
    bound: the reading is missing, not the training (INV-005).
    """
    if not captured:
        return Streak(days=0, start=None, end=None)

    best = Streak(days=0, start=None, end=None)
    run: list[date] = []

    def close(run: list[date], best: Streak) -> Streak:
        if not run or len(run) <= best.days:
            return best
        touches_edge = (
            run[0] - timedelta(days=1) not in captured
            or run[-1] + timedelta(days=1) not in captured
        )
        return Streak(days=len(run), start=run[0], end=run[-1], is_lower_bound=touches_edge)

    cursor, last = min(captured), max(captured)
    while cursor <= last:
        if cursor in trained:
            run.append(cursor)
        elif breaks(cursor) or cursor not in captured:
            best = close(run, best)
            run = []
        cursor += timedelta(days=1)

    return close(run, best)


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
    "Abandonment",
    "Calendar",
    "CancellationBands",
    "CountedRecord",
    "CountedState",
    "DayCell",
    "DayStatus",
    "GridCell",
    "LeadBands",
    "MonthPoint",
    "Occupancy",
    "SlotStats",
    "Streak",
    "Streaks",
    "abandonment",
    "booking_lead_bands",
    "cancellation_bands",
    "counted_records",
    "day_calendar",
    "drop_rate_by_slot",
    "monthly_trend",
    "occupancy",
    "settle_cutoff",
    "streaks",
    "weekday_hour_grid",
]
