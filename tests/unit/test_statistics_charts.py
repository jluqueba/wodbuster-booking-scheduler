"""Unit tests for the chart metrics and the global period (slice 5).

Pure functions over records, so every rule is asserted directly rather
than through a rendered page.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from wodbuster_worker.persistence.models import AttendanceRecord
from wodbuster_worker.statistics.metrics import (
    booking_lead_bands,
    counted_records,
    drop_rate_by_slot,
    monthly_trend,
    occupancy,
    weekday_hour_grid,
)
from wodbuster_worker.statistics.periods import DEFAULT_PERIOD, resolve_period

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
SETTLE = 3.0
TODAY = date(2026, 9, 25)
HORIZON = 365


def _record(
    *,
    day: date,
    hour: int,
    minute: int = 30,
    state: str = "attended",
    lead_hours: float | None = 72.0,
    capacity: int | None = 14,
    occupancy_n: int = 14,
    ever_full: bool = True,
) -> AttendanceRecord:
    # Built in UTC and asserted in Europe/Madrid, which is what every
    # by-slot metric reads; a naive build would hide an offset bug.
    start = datetime(day.year, day.month, day.day, hour, minute, tzinfo=UTC)
    return AttendanceRecord(
        gym_account_id=1,
        local_date=day,
        wodbuster_class_id=int(start.timestamp()) % 1000000,
        class_name="Cross Training",
        class_type_id=1,
        start_at=start,
        state=state,
        state_changed_at=(None if lead_hours is None else start - timedelta(hours=lead_hours)),
        reservation_type="Tarifa",
        capacity=capacity,
        occupancy=occupancy_n,
        ever_full=ever_full,
    )


def _counted(*records: AttendanceRecord):
    return counted_records(list(records), now=NOW, settle_window_hours=SETTLE)


# ---------------------------------------------------------------------------
# Weekday by hour grid
# ---------------------------------------------------------------------------


def test_the_grid_buckets_by_the_operator_clock_not_utc() -> None:
    """CC-044: A stored 18:30 UTC in September is 20:30 in Madrid.

    Bucketing on the raw instant would move every evening session two
    hours earlier and quietly invent a different routine.
    """
    # 2026-09-21 is a Monday.
    cells = weekday_hour_grid(_counted(_record(day=date(2026, 9, 21), hour=18)))

    assert len(cells) == 1
    assert (cells[0].weekday, cells[0].slot, cells[0].attended) == (0, "20:30", 1)


def test_the_grid_keeps_the_minutes_of_the_class_time() -> None:
    """CC-043: Rounding to the hour would merge distinct slots and label a
    20:30 session as 20:00, which is simply not true. This gym also
    runs a 10:40, so the minutes are not always 00 or 30."""
    cells = weekday_hour_grid(
        _counted(
            _record(day=date(2026, 9, 21), hour=8),
            _record(day=date(2026, 9, 22), hour=8, minute=40),
        )
    )

    assert sorted(cell.slot for cell in cells) == ["10:30", "10:40"]


def test_the_grid_counts_only_attended_sessions() -> None:
    day = date(2026, 9, 21)
    cells = weekday_hour_grid(
        _counted(
            _record(day=day, hour=18),
            _record(day=date(2026, 9, 22), hour=18, state="cancelled", lead_hours=48),
        )
    )

    assert sum(cell.attended for cell in cells) == 1


def test_the_grid_is_sparse() -> None:
    """Only populated buckets are returned, so the template decides how
    an empty Sunday looks instead of this inventing a row of zeros."""
    cells = weekday_hour_grid(_counted(_record(day=date(2026, 9, 21), hour=18)))

    assert len(cells) == 1


def test_an_empty_grid_is_empty() -> None:
    assert weekday_hour_grid([]) == ()


# ---------------------------------------------------------------------------
# Drop rate by hour
# ---------------------------------------------------------------------------


def test_the_drop_rate_is_computed_per_slot() -> None:
    records = [_record(day=date(2026, 9, d), hour=18) for d in range(1, 4)]
    records.append(_record(day=date(2026, 9, 4), hour=18, state="cancelled", lead_hours=48))

    stats = drop_rate_by_slot(_counted(*records), min_bookings=1)

    assert len(stats) == 1
    assert (stats[0].slot, stats[0].attended, stats[0].cancelled) == ("20:30", 3, 1)
    assert stats[0].drop_rate == 0.25


def test_slots_with_too_little_history_are_hidden() -> None:
    """CC-045: One drop out of one booking is a 100 percent rate and a
    meaningless one."""
    records = [_record(day=date(2026, 9, d), hour=18) for d in range(1, 6)]
    records.append(_record(day=date(2026, 9, 6), hour=10))

    stats = drop_rate_by_slot(_counted(*records), min_bookings=5)

    assert [stat.slot for stat in stats] == ["20:30"]


def test_a_class_change_is_not_a_drop_in_the_by_slot_view() -> None:
    """The reclassification applies here too, or this chart would
    disagree with the headline rate."""
    day = date(2026, 9, 21)
    records = [
        _record(day=day, hour=16, state="cancelled", lead_hours=2),
        _record(day=day, hour=18),
    ]

    stats = drop_rate_by_slot(_counted(*records), min_bookings=1)

    assert all(stat.cancelled == 0 for stat in stats)


# ---------------------------------------------------------------------------
# Monthly trend
# ---------------------------------------------------------------------------


def test_the_trend_keeps_months_with_no_activity() -> None:
    """A month off is information; compressing it would draw a flatter
    picture than the truth."""
    points = monthly_trend(
        _counted(
            _record(day=date(2026, 1, 5), hour=18),
            _record(day=date(2026, 4, 5), hour=18),
        )
    )

    assert [p.key for p in points] == ["2026-01", "2026-02", "2026-03", "2026-04"]
    assert [p.attended for p in points] == [1, 0, 0, 1]


def test_the_trend_crosses_a_year_boundary() -> None:
    points = monthly_trend(
        _counted(
            _record(day=date(2025, 12, 5), hour=18),
            _record(day=date(2026, 1, 5), hour=18),
        )
    )

    assert [p.key for p in points] == ["2025-12", "2026-01"]


def test_an_empty_trend_is_empty() -> None:
    assert monthly_trend([]) == ()


# ---------------------------------------------------------------------------
# Booking lead
# ---------------------------------------------------------------------------


def test_bookings_are_grouped_by_how_far_ahead_they_were_made() -> None:
    bands = booking_lead_bands(
        _counted(
            _record(day=date(2026, 9, 1), hour=18, lead_hours=2),
            _record(day=date(2026, 9, 2), hour=18, lead_hours=10),
            _record(day=date(2026, 9, 3), hour=18, lead_hours=100),
        ),
        free_hours=4.0,
    )

    assert (bands.same_day, bands.within_day, bands.early) == (1, 1, 1)
    assert bands.total == 3


def test_the_free_window_boundary_is_the_gym_threshold() -> None:
    """Exactly at the threshold is outside the free window, matching
    the gym's "under four hours is free" wording."""
    exactly = booking_lead_bands(
        _counted(_record(day=date(2026, 9, 1), hour=18, lead_hours=4)),
        free_hours=4.0,
    )

    assert (exactly.same_day, exactly.within_day) == (0, 1)


def test_only_attended_classes_count_as_bookings_kept() -> None:
    bands = booking_lead_bands(
        _counted(_record(day=date(2026, 9, 1), hour=18, state="cancelled", lead_hours=2)),
        free_hours=4.0,
    )

    assert bands.total == 0


def test_a_booking_with_no_instant_is_reported_not_guessed() -> None:
    bands = booking_lead_bands(
        _counted(_record(day=date(2026, 9, 1), hour=18, lead_hours=None)),
        free_hours=4.0,
    )

    assert bands.unknown == 1
    assert (bands.same_day, bands.within_day, bands.early) == (0, 0, 0)


# ---------------------------------------------------------------------------
# Occupancy
# ---------------------------------------------------------------------------


def test_occupancy_averages_the_fill_of_attended_classes() -> None:
    result = occupancy(
        _counted(
            _record(day=date(2026, 9, 1), hour=18, capacity=10, occupancy_n=10),
            _record(day=date(2026, 9, 2), hour=18, capacity=10, occupancy_n=6),
        )
    )

    assert result.sessions == 2
    assert result.average_fill == 0.8


def test_ever_full_uses_the_upstream_flag_not_a_comparison() -> None:
    """Occupancy is a snapshot at capture time; the flag is the only
    reliable answer to "did this class fill up"."""
    result = occupancy(
        _counted(_record(day=date(2026, 9, 1), hour=18, capacity=14, occupancy_n=9, ever_full=True))
    )

    assert result.ever_full == 1
    assert result.ever_full_share == 1.0


def test_a_class_with_no_capacity_does_not_break_the_average() -> None:
    result = occupancy(
        _counted(
            _record(day=date(2026, 9, 1), hour=18, capacity=None, occupancy_n=9),
            _record(day=date(2026, 9, 2), hour=18, capacity=10, occupancy_n=5),
        )
    )

    assert result.sessions == 2
    assert result.average_fill == 0.5


def test_occupancy_over_nothing_is_unknown_not_zero() -> None:
    result = occupancy([])

    assert result.sessions == 0
    assert result.average_fill is None
    assert result.ever_full_share is None


# ---------------------------------------------------------------------------
# Period selector
# ---------------------------------------------------------------------------


def _period(key: str | None, oldest: date | None = date(2025, 9, 24)):
    return resolve_period(key, today=TODAY, horizon_days=HORIZON, oldest_captured=oldest)


def test_each_period_covers_its_own_span() -> None:
    assert _period("m1").days == 30
    assert _period("m3").days == 91
    assert _period("m12").days == 365


def test_an_unknown_period_falls_back_to_the_default() -> None:
    for value in ("", "nonsense", "m99", None):
        assert _period(value).key == DEFAULT_PERIOD


def test_everything_is_bounded_by_what_was_captured() -> None:
    """The label must not promise history the backfill has not reached."""
    window = _period("all", oldest=date(2026, 6, 1))

    assert window.start == date(2026, 6, 1)


def test_everything_degrades_to_the_horizon_when_nothing_is_captured() -> None:
    window = _period("all", oldest=None)

    assert window.start == TODAY - timedelta(days=HORIZON)


def test_no_period_reaches_past_the_horizon() -> None:
    """Days older than the horizon are never captured, so including
    them would dilute every average with guaranteed silence."""
    window = resolve_period("all", today=TODAY, horizon_days=30, oldest_captured=date(2020, 1, 1))

    assert window.start == TODAY - timedelta(days=30)


def test_the_billing_period_is_offered_when_the_gym_stated_it() -> None:
    window = resolve_period(
        "billing",
        today=TODAY,
        horizon_days=HORIZON,
        oldest_captured=None,
        billing=(date(2026, 9, 7), date(2026, 10, 6)),
    )

    assert window.key == "billing"
    assert window.start == date(2026, 9, 7)


def test_an_open_billing_period_stops_at_today() -> None:
    """Charts describe what happened. Running the window to the end of
    a period still in progress would average real days against days
    that have not occurred."""
    window = resolve_period(
        "billing",
        today=TODAY,
        horizon_days=HORIZON,
        oldest_captured=None,
        billing=(date(2026, 9, 7), date(2026, 10, 6)),
    )

    assert window.end == TODAY


def test_asking_for_a_billing_period_the_gym_did_not_state_falls_back() -> None:
    """FR-031 at this boundary: omit without error."""
    window = resolve_period(
        "billing",
        today=TODAY,
        horizon_days=HORIZON,
        oldest_captured=None,
        billing=None,
    )

    assert window.key == DEFAULT_PERIOD
