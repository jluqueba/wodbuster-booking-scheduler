"""Unit tests for the metric boundary rules and the attendance series.

The functions under test are pure, so these run without a database and
without a browser, which is the point of keeping the arithmetic in
Python (ADR-0014, Decision 2).
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from wodbuster_worker.persistence.models import AttendanceRecord
from wodbuster_worker.statistics.metrics import (
    counted_records,
    day_calendar,
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
