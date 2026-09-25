"""Unit tests for the metric boundary rules and the attendance series.

The functions under test are pure, so these run without a database and
without a browser, which is the point of keeping the arithmetic in
Python (ADR-0014, Decision 2).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping
from datetime import UTC, date, datetime, timedelta

from wodbuster_worker.persistence.models import AttendanceRecord
from wodbuster_worker.statistics.metrics import (
    CancellationBands,
    CountedRecord,
    Streaks,
    abandonment,
    cancellation_bands,
    counted_records,
    day_calendar,
    streaks,
)

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
SETTLE = 3.0


def _record(
    *,
    start_at: datetime,
    state: str = "attended",
    state_changed_at: datetime | None = None,
    local_date: date | None = None,
) -> AttendanceRecord:
    return AttendanceRecord(
        gym_account_id=1,
        local_date=local_date if local_date is not None else start_at.date(),
        wodbuster_class_id=int(start_at.timestamp()) % 100000,
        class_name="Cross Training",
        class_type_id=1,
        start_at=start_at,
        state=state,
        state_changed_at=state_changed_at,
        reservation_type="Tarifa",
        capacity=14,
        occupancy=14,
        ever_full=True,
    )


# ---------------------------------------------------------------------------
# Settle window (INV-003, CC-009, CC-025)
# ---------------------------------------------------------------------------


def test_a_class_that_has_not_started_does_not_count() -> None:
    tonight = _record(start_at=NOW + timedelta(hours=8))

    assert counted_records([tonight], now=NOW, settle_window_hours=SETTLE) == []


def test_a_class_that_started_twenty_minutes_ago_does_not_count() -> None:
    """CC-009: the coach can still remove a no-show at that point."""
    just_started = _record(start_at=NOW - timedelta(minutes=20))

    assert counted_records([just_started], now=NOW, settle_window_hours=SETTLE) == []


def test_a_class_past_its_settle_window_counts() -> None:
    settled = _record(start_at=NOW - timedelta(hours=3, minutes=1))

    assert len(counted_records([settled], now=NOW, settle_window_hours=SETTLE)) == 1


def test_the_settle_boundary_is_exact() -> None:
    """Asserted at the boundary so it is a decision, not an accident."""
    exactly = _record(start_at=NOW - timedelta(hours=3))

    assert len(counted_records([exactly], now=NOW, settle_window_hours=SETTLE)) == 1


def test_the_settle_window_is_configurable() -> None:
    record = _record(start_at=NOW - timedelta(hours=2))

    assert counted_records([record], now=NOW, settle_window_hours=SETTLE) == []
    assert len(counted_records([record], now=NOW, settle_window_hours=1.0)) == 1


# ---------------------------------------------------------------------------
# Removal after the class began (INV-010, CC-024)
# ---------------------------------------------------------------------------


def test_a_removal_before_the_class_is_a_cancellation() -> None:
    start = NOW - timedelta(days=1)
    record = _record(
        start_at=start,
        state="cancelled",
        state_changed_at=start - timedelta(hours=2),
    )

    counted = counted_records([record], now=NOW, settle_window_hours=SETTLE)

    assert counted[0].state == "cancelled"
    assert counted[0].cancellation_lead == timedelta(hours=2)


def test_a_removal_after_the_class_started_is_its_own_category() -> None:
    start = NOW - timedelta(days=1)
    record = _record(
        start_at=start,
        state="cancelled",
        state_changed_at=start + timedelta(minutes=10),
    )

    counted = counted_records([record], now=NOW, settle_window_hours=SETTLE)

    assert counted[0].state == "removed_after_start"
    assert counted[0].cancellation_lead is None


def test_a_removal_exactly_at_the_class_start_is_not_a_cancellation() -> None:
    """The boundary belongs to the category that is not the user's fault."""
    start = NOW - timedelta(days=1)
    record = _record(start_at=start, state="cancelled", state_changed_at=start)

    counted = counted_records([record], now=NOW, settle_window_hours=SETTLE)

    assert counted[0].state == "removed_after_start"


def test_a_cancellation_without_an_instant_stays_a_cancellation() -> None:
    """It loses its band, not its place in the abandonment rate."""
    record = _record(
        start_at=NOW - timedelta(days=1),
        state="cancelled",
        state_changed_at=None,
    )

    counted = counted_records([record], now=NOW, settle_window_hours=SETTLE)

    assert counted[0].state == "cancelled"
    assert counted[0].cancellation_lead is None


def test_a_no_show_is_carried_through_unchanged() -> None:
    record = _record(start_at=NOW - timedelta(days=1), state="no_show")

    assert counted_records([record], now=NOW, settle_window_hours=SETTLE)[0].state == "no_show"


# ---------------------------------------------------------------------------
# Class changes (the abandonment rate depends on this)
# ---------------------------------------------------------------------------


def test_a_removal_on_a_day_you_trained_is_a_class_change() -> None:
    """The real 11/09 case: moved from 16:30 to 17:30.

    The rule is the owner's definition of the metric: "I booked and
    ended up not training". On a day holding a training, a removal is a
    move.
    """
    day = date(2026, 9, 11)
    moment = datetime(2026, 9, 11, 14, 21, 40, tzinfo=UTC)
    records = counted_records(
        [
            _record(
                start_at=datetime(2026, 9, 11, 16, 30, tzinfo=UTC),
                local_date=day,
                state="cancelled",
                state_changed_at=moment,
            ),
            _record(
                start_at=datetime(2026, 9, 11, 17, 30, tzinfo=UTC),
                local_date=day,
                state="attended",
                state_changed_at=moment,
            ),
        ],
        now=NOW,
        settle_window_hours=SETTLE,
    )

    assert sorted(r.state for r in records) == ["attended", "swapped"]


def test_a_removal_is_a_class_change_even_when_the_acts_are_days_apart() -> None:
    """The real 18/09 case: cancelled two days early, booked later.

    An earlier rule matched the identical upstream state instant, which
    is more precise about the mechanics and answers the wrong question.
    The user trained that day, so the seat they released is not an
    abandonment by the definition this metric carries.
    """
    day = date(2026, 9, 18)
    records = counted_records(
        [
            _record(
                start_at=datetime(2026, 9, 18, 16, 30, tzinfo=UTC),
                local_date=day,
                state="cancelled",
                state_changed_at=datetime(2026, 9, 16, 16, 1, tzinfo=UTC),
            ),
            _record(
                start_at=datetime(2026, 9, 18, 12, 30, tzinfo=UTC),
                local_date=day,
                state="attended",
                state_changed_at=datetime(2026, 9, 18, 12, 8, tzinfo=UTC),
            ),
        ],
        now=NOW,
        settle_window_hours=SETTLE,
    )

    assert sorted(r.state for r in records) == ["attended", "swapped"]


def test_a_removal_on_a_day_you_did_not_train_is_a_drop() -> None:
    day = date(2026, 9, 21)
    records = counted_records(
        [
            _record(
                start_at=datetime(2026, 9, 21, 18, 30, tzinfo=UTC),
                local_date=day,
                state="cancelled",
                state_changed_at=datetime(2026, 9, 21, 16, 29, tzinfo=UTC),
            )
        ],
        now=NOW,
        settle_window_hours=SETTLE,
    )

    assert [r.state for r in records] == ["cancelled"]


def test_training_on_a_different_day_does_not_excuse_a_drop() -> None:
    records = counted_records(
        [
            _record(
                start_at=datetime(2026, 9, 11, 16, 30, tzinfo=UTC),
                local_date=date(2026, 9, 11),
                state="cancelled",
                state_changed_at=datetime(2026, 9, 11, 14, 0, tzinfo=UTC),
            ),
            _record(
                start_at=datetime(2026, 9, 12, 17, 30, tzinfo=UTC),
                local_date=date(2026, 9, 12),
                state="attended",
                state_changed_at=datetime(2026, 9, 11, 14, 0, tzinfo=UTC),
            ),
        ],
        now=NOW,
        settle_window_hours=SETTLE,
    )

    assert sorted(r.state for r in records) == ["attended", "cancelled"]


def test_an_absence_is_not_excused_by_training_later_that_day() -> None:
    """Removed once the class had started, then trained something else.

    That is still an absence from the first class. The order of the
    rules is what keeps it one.
    """
    day = date(2026, 9, 11)
    records = counted_records(
        [
            _record(
                start_at=datetime(2026, 9, 11, 9, 0, tzinfo=UTC),
                local_date=day,
                state="cancelled",
                state_changed_at=datetime(2026, 9, 11, 9, 10, tzinfo=UTC),
            ),
            _record(
                start_at=datetime(2026, 9, 11, 17, 30, tzinfo=UTC),
                local_date=day,
                state="attended",
                state_changed_at=datetime(2026, 9, 11, 14, 0, tzinfo=UTC),
            ),
        ],
        now=NOW,
        settle_window_hours=SETTLE,
    )

    assert sorted(r.state for r in records) == ["attended", "removed_after_start"]


# ---------------------------------------------------------------------------
# Calendar
# ---------------------------------------------------------------------------


def _calendar_for(
    records: list[AttendanceRecord],
    captured: dict[date, int],
    start: date,
    end: date,
    today: date = date(2026, 9, 30),
):
    return day_calendar(
        counted_records(records, now=NOW, settle_window_hours=SETTLE),
        captured=captured,
        start=start,
        end=end,
        today=today,
    )


def test_calendar_weeks_always_have_seven_columns() -> None:
    # 2026-09-01 is a Tuesday, so the first week needs one pad cell.
    calendar = _calendar_for([], {}, date(2026, 9, 1), date(2026, 9, 14))

    assert all(len(week) == 7 for week in calendar.weeks)
    assert calendar.weeks[0][0] is None
    assert calendar.weeks[0][1] is not None
    assert calendar.weeks[0][1].day == date(2026, 9, 1)


def test_calendar_separates_closed_from_idle_from_unread() -> None:
    """INV-005: the cases a bar chart cannot tell apart."""
    start, end = date(2026, 9, 14), date(2026, 9, 16)
    calendar = _calendar_for(
        [],
        {date(2026, 9, 14): 0, date(2026, 9, 15): 20},
        start,
        end,
    )
    cells = {cell.day: cell for week in calendar.weeks for cell in week if cell is not None}

    assert cells[date(2026, 9, 14)].status == "closed"
    assert cells[date(2026, 9, 15)].status == "no_activity"
    assert cells[date(2026, 9, 16)].status == "uncaptured"


def test_a_day_that_has_not_happened_is_not_a_day_we_failed_to_read() -> None:
    """Whole months show future days; they are not gaps in coverage."""
    start, end = date(2026, 9, 24), date(2026, 9, 26)
    calendar = _calendar_for([], {}, start, end, today=date(2026, 9, 24))
    cells = {cell.day: cell for week in calendar.weeks for cell in week if cell is not None}

    assert cells[date(2026, 9, 24)].status == "uncaptured"
    assert cells[date(2026, 9, 25)].status == "future"
    assert cells[date(2026, 9, 26)].status == "future"


def test_a_dropped_day_is_visible_in_the_calendar() -> None:
    """The 21/09 complaint: a drop must not look like an idle day."""
    day = date(2026, 9, 21)
    calendar = _calendar_for(
        [
            _record(
                start_at=datetime(2026, 9, 21, 18, 30, tzinfo=UTC),
                local_date=day,
                state="cancelled",
                state_changed_at=datetime(2026, 9, 21, 16, 29, tzinfo=UTC),
            )
        ],
        {day: 20},
        day,
        day,
    )
    cell = calendar.weeks[0][day.weekday()]

    assert cell is not None
    assert cell.status == "cancelled"
    assert cell.cancelled == 1
    assert calendar.cancelled == 1


def test_a_day_with_a_class_change_is_painted_as_a_change() -> None:
    """The change wins the cell colour; the training is still counted.

    A marker inside an otherwise green cell was too easy to miss, and
    the tile could report two changes while no cell showed where.
    """
    day = date(2026, 9, 11)
    moment = datetime(2026, 9, 11, 14, 21, tzinfo=UTC)
    calendar = _calendar_for(
        [
            _record(
                start_at=datetime(2026, 9, 11, 16, 30, tzinfo=UTC),
                local_date=day,
                state="cancelled",
                state_changed_at=moment,
            ),
            _record(
                start_at=datetime(2026, 9, 11, 17, 30, tzinfo=UTC),
                local_date=day,
                state="attended",
                state_changed_at=moment,
            ),
        ],
        {day: 20},
        day,
        day,
    )
    cell = calendar.weeks[0][day.weekday()]

    assert cell is not None
    assert cell.status == "swapped"
    assert cell.attended == 1
    assert cell.swapped == 1
    assert calendar.attended == 1
    assert calendar.cancelled == 0
    assert calendar.swapped == 1


def test_a_day_holding_a_training_and_a_drop_reports_both() -> None:
    """A drop the user did not make on a day they trained is a change.

    The real 18/09: dropped the 16:30 two days earlier, trained at
    12:30. The day is a class change, so the drop tile must not claim
    a red cell the reader cannot find.
    """
    day = date(2026, 9, 18)
    calendar = _calendar_for(
        [
            _record(
                start_at=datetime(2026, 9, 18, 14, 30, tzinfo=UTC),
                local_date=day,
                state="cancelled",
                state_changed_at=datetime(2026, 9, 16, 14, 1, tzinfo=UTC),
            ),
            _record(
                start_at=datetime(2026, 9, 18, 10, 30, tzinfo=UTC),
                local_date=day,
                state="attended",
                state_changed_at=datetime(2026, 9, 18, 10, 8, tzinfo=UTC),
            ),
        ],
        {day: 20},
        day,
        day,
    )
    cell = calendar.weeks[0][day.weekday()]

    assert cell is not None
    assert cell.status == "swapped"
    assert cell.attended == 1
    assert cell.swapped == 1
    assert cell.cancelled == 0
    assert cell.attended_names == ("Cross Training",)
    assert calendar.attended == 1
    assert calendar.cancelled == 0
    assert calendar.swapped == 1


def test_an_empty_range_yields_an_empty_calendar() -> None:
    calendar = _calendar_for([], {}, date(2026, 9, 24), date(2026, 9, 23))

    assert calendar.weeks == ()
    assert calendar.attended == 0


# ---------------------------------------------------------------------------
# Abandonment rate
# ---------------------------------------------------------------------------


def _counted(*records: AttendanceRecord) -> list[CountedRecord]:
    return counted_records(list(records), now=NOW, settle_window_hours=SETTLE)


def _drop(day: date, *, hours_before: float | None = 2.0) -> AttendanceRecord:
    start = datetime(day.year, day.month, day.day, 18, 30, tzinfo=UTC)
    return _record(
        start_at=start,
        local_date=day,
        state="cancelled",
        state_changed_at=(None if hours_before is None else start - timedelta(hours=hours_before)),
    )


def _trained(day: date, hour: int = 18) -> AttendanceRecord:
    return _record(
        start_at=datetime(day.year, day.month, day.day, hour, 30, tzinfo=UTC),
        local_date=day,
    )


def test_the_rate_is_dropped_over_bookings_that_resolved() -> None:
    records = _counted(
        *(_trained(date(2026, 9, d)) for d in range(1, 9)),
        _drop(date(2026, 9, 10)),
        _drop(date(2026, 9, 11)),
    )

    result = abandonment(records)

    assert (result.attended, result.cancelled, result.booked) == (8, 2, 10)
    assert result.rate == 0.2
    assert result.percent == 20


def test_a_rate_over_nothing_is_unknown_not_zero() -> None:
    """Reporting perfect behaviour to someone who booked nothing is
    worse than an honest dash."""
    result = abandonment([])

    assert result.booked == 0
    assert result.rate is None
    assert result.percent is None


def test_a_class_change_is_in_neither_half_of_the_rate() -> None:
    day = date(2026, 9, 11)
    records = _counted(_drop(day, hours_before=4.0), _trained(day, hour=20))

    result = abandonment(records)

    assert result.swapped == 1
    assert (result.attended, result.cancelled) == (1, 0)
    assert result.percent == 0


def test_absences_are_reported_but_left_out_of_the_rate() -> None:
    """They are not drop-outs, and folding them in would answer a
    different question than the one the tile asks."""
    records = _counted(
        _trained(date(2026, 9, 1)),
        _record(
            start_at=datetime(2026, 9, 2, 18, 30, tzinfo=UTC),
            local_date=date(2026, 9, 2),
            state="no_show",
        ),
    )

    result = abandonment(records)

    assert result.no_show == 1
    assert result.booked == 1
    assert result.percent == 0


def test_the_percent_rounds_half_up() -> None:
    """A reader checking 1 of 8 against the tile should not meet
    banker's rounding."""
    records = _counted(
        *(_trained(date(2026, 9, d)) for d in range(1, 8)),
        _drop(date(2026, 9, 9)),
    )

    # 1/8 is 12.5 percent exactly.
    assert abandonment(records).percent == 13


# ---------------------------------------------------------------------------
# Cancellation bands
# ---------------------------------------------------------------------------


def _bands(*records: AttendanceRecord) -> CancellationBands:
    return cancellation_bands(_counted(*records), late_hours=4.0, very_late_hours=1.0)


def test_each_band_catches_its_own_notice_period() -> None:
    result = _bands(
        _drop(date(2026, 9, 1), hours_before=30.0),
        _drop(date(2026, 9, 2), hours_before=2.0),
        _drop(date(2026, 9, 3), hours_before=0.5),
    )

    assert (result.early, result.late, result.very_late) == (1, 1, 1)
    assert result.total == 3


def test_the_observed_case_lands_between_one_and_four_hours() -> None:
    """The real 21/09: removed at 18:29 from a class starting 20:30."""
    start = datetime(2026, 9, 21, 20, 30, tzinfo=UTC)
    result = cancellation_bands(
        _counted(
            _record(
                start_at=start,
                local_date=date(2026, 9, 21),
                state="cancelled",
                state_changed_at=datetime(2026, 9, 21, 18, 29, tzinfo=UTC),
            )
        ),
        late_hours=4.0,
        very_late_hours=1.0,
    )

    assert (result.early, result.late, result.very_late) == (0, 1, 0)


def test_a_boundary_counts_as_the_more_generous_band() -> None:
    """The gym defines "more than four hours" and "less than four
    hours" and leaves four hours itself undefined, so the tie goes to
    the user rather than to an arbitrary choice."""
    exactly_four = _bands(_drop(date(2026, 9, 1), hours_before=4.0))
    exactly_one = _bands(_drop(date(2026, 9, 2), hours_before=1.0))

    assert (exactly_four.early, exactly_four.late) == (1, 0)
    assert (exactly_one.late, exactly_one.very_late) == (1, 0)


def test_a_cancellation_with_no_instant_is_reported_not_hidden() -> None:
    """It still happened. Folding it into a band would invent a notice
    period; dropping it would make the bands disagree with the rate."""
    result = _bands(_drop(date(2026, 9, 1), hours_before=None))

    assert result.unknown_lead == 1
    assert (result.early, result.late, result.very_late) == (0, 0, 0)
    assert result.total == 1


def test_the_bands_and_the_rate_agree_on_the_same_records() -> None:
    records = (
        _drop(date(2026, 9, 1), hours_before=30.0),
        _drop(date(2026, 9, 2), hours_before=2.0),
        _drop(date(2026, 9, 3), hours_before=None),
        _trained(date(2026, 9, 4)),
    )

    counted = _counted(*records)

    assert cancellation_bands(counted, late_hours=4.0, very_late_hours=1.0).total == 3
    assert abandonment(counted).cancelled == 3


def test_the_thresholds_are_arguments_not_constants() -> None:
    """A second gym will not share Antwork's penalty tiers."""
    drop = _drop(date(2026, 9, 1), hours_before=6.0)

    antwork = cancellation_bands(_counted(drop), late_hours=4.0, very_late_hours=1.0)
    stricter = cancellation_bands(_counted(drop), late_hours=12.0, very_late_hours=3.0)

    assert (antwork.early, antwork.late) == (1, 0)
    assert (stricter.early, stricter.late) == (0, 1)


def test_only_voluntary_cancellations_are_banded() -> None:
    day = date(2026, 9, 11)
    start = datetime(2026, 9, 11, 18, 30, tzinfo=UTC)
    result = _bands(
        _record(
            start_at=start,
            local_date=day,
            state="cancelled",
            state_changed_at=start + timedelta(minutes=10),
        )
    )

    assert result.total == 0


# ---------------------------------------------------------------------------
# Streaks (ADR-0015 Decision 1)
# ---------------------------------------------------------------------------


def _ledger(start: date, days: int, *, closed: Collection[date] = ()) -> dict[date, int]:
    """A contiguous ledger: every day read, closed days holding zero classes."""
    return {
        start + timedelta(days=offset): (0 if start + timedelta(days=offset) in closed else 8)
        for offset in range(days)
    }


def _streaks(
    *trained_days: date,
    ledger: Mapping[date, int],
    today: date,
    excluded: Collection[int] = (),
) -> Streaks:
    # The clock sits inside the day under test, otherwise the settle
    # window would silently drop the most recent trainings.
    now = datetime(today.year, today.month, today.day, 12, tzinfo=UTC)
    records = counted_records(
        [_trained(day) for day in trained_days],
        now=now,
        settle_window_hours=SETTLE,
    )
    return streaks(records, captured=ledger, today=today, excluded_weekdays=excluded)


def test_consecutive_training_days_form_a_streak() -> None:
    ledger = _ledger(date(2026, 9, 20), 6)

    result = _streaks(
        date(2026, 9, 22),
        date(2026, 9, 23),
        date(2026, 9, 24),
        ledger=ledger,
        today=date(2026, 9, 25),
    )

    assert result.current.days == 3
    assert (result.current.start, result.current.end) == (date(2026, 9, 22), date(2026, 9, 24))


def test_a_day_the_gym_ran_no_classes_does_not_break_a_streak() -> None:
    """One rule covers the weekly closing day and a public holiday
    alike, so neither has to be configured anywhere."""
    sunday = date(2026, 9, 20)
    ledger = _ledger(date(2026, 9, 14), 12, closed={sunday})

    result = _streaks(
        date(2026, 9, 19),
        date(2026, 9, 21),
        ledger=ledger,
        today=date(2026, 9, 22),
    )

    assert result.current.days == 2


def test_a_closed_day_does_not_add_to_the_streak_either() -> None:
    """Counting it would claim a session on a day the gym was shut."""
    sunday = date(2026, 9, 20)
    ledger = _ledger(date(2026, 9, 14), 12, closed={sunday})

    result = _streaks(
        date(2026, 9, 19),
        date(2026, 9, 21),
        ledger=ledger,
        today=date(2026, 9, 22),
    )

    assert result.current.days == 2
    assert (result.current.start, result.current.end) == (date(2026, 9, 19), date(2026, 9, 21))


def test_an_open_day_you_skipped_breaks_the_streak() -> None:
    ledger = _ledger(date(2026, 9, 14), 12)

    result = _streaks(
        date(2026, 9, 19),
        date(2026, 9, 21),
        ledger=ledger,
        today=date(2026, 9, 22),
    )

    assert result.current.days == 1
    assert result.current.start == date(2026, 9, 21)


def test_a_weekday_you_never_train_does_not_break_the_streak() -> None:
    """The gym opens on Sunday, the user never goes. Without this the
    streak would reset every week and the figure would say nothing."""
    ledger = _ledger(date(2026, 9, 14), 12)
    sunday = date(2026, 9, 20).weekday()

    result = _streaks(
        date(2026, 9, 19),
        date(2026, 9, 21),
        ledger=ledger,
        today=date(2026, 9, 22),
        excluded=(sunday,),
    )

    assert result.current.days == 2


def test_today_does_not_break_the_streak_before_the_day_is_over() -> None:
    """Otherwise the figure would reset every morning before the gym
    opens and recover in the evening."""
    ledger = _ledger(date(2026, 9, 20), 6)

    result = _streaks(
        date(2026, 9, 23),
        date(2026, 9, 24),
        ledger=ledger,
        today=date(2026, 9, 25),
    )

    assert result.current.days == 2


def test_a_run_reaching_the_oldest_reading_is_reported_as_a_lower_bound() -> None:
    """Absence of a reading is not absence of training (INV-005), so
    the page says "at least" instead of naming a number nobody
    measured."""
    ledger = _ledger(date(2026, 9, 22), 4)

    result = _streaks(
        date(2026, 9, 22),
        date(2026, 9, 23),
        date(2026, 9, 24),
        ledger=ledger,
        today=date(2026, 9, 25),
    )

    assert result.current.days == 3
    assert result.current.is_lower_bound is True
    assert result.longest_is_provisional is True


def test_a_run_inside_the_read_history_is_a_fact_not_a_bound() -> None:
    ledger = _ledger(date(2026, 9, 14), 12)

    result = _streaks(
        date(2026, 9, 19),
        date(2026, 9, 20),
        date(2026, 9, 21),
        ledger=ledger,
        today=date(2026, 9, 25),
    )

    assert result.longest.days == 3
    assert result.longest.is_lower_bound is False
    assert result.longest_is_provisional is False


def test_the_longest_run_is_found_anywhere_in_the_history() -> None:
    ledger = _ledger(date(2026, 9, 1), 25)

    result = _streaks(
        date(2026, 9, 3),
        date(2026, 9, 4),
        date(2026, 9, 5),
        date(2026, 9, 6),
        date(2026, 9, 24),
        ledger=ledger,
        today=date(2026, 9, 25),
    )

    assert result.current.days == 1
    assert result.longest.days == 4
    assert (result.longest.start, result.longest.end) == (date(2026, 9, 3), date(2026, 9, 6))


def test_no_training_at_all_leaves_both_runs_at_zero() -> None:
    result = streaks([], captured=_ledger(date(2026, 9, 1), 25), today=date(2026, 9, 25))

    assert (result.current.days, result.longest.days) == (0, 0)
    assert result.current.start is None


def test_an_empty_ledger_yields_no_streak_rather_than_an_error() -> None:
    result = streaks([], captured={}, today=date(2026, 9, 25))

    assert (result.current.days, result.longest.days) == (0, 0)
