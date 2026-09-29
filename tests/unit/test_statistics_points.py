"""Unit tests for the points model and the weekly comparison.

The points rules under test are the gym's own, quoted in
``statistics/points.py``. What these assert is the separation the
feature depends on: a penalty is derived from an instant the gym
recorded and is a fact, while the base cost of a dropped booking cannot
be known and therefore never collapses into a single number.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

from wodbuster_worker.persistence.models import AttendanceRecord
from wodbuster_worker.statistics.metrics import counted_records, weekly_average
from wodbuster_worker.statistics.points import PointsModel, points_estimate

NOW = datetime(2026, 9, 25, 23, 0, tzinfo=UTC)
SETTLE = 3.0

# Antwork's published values, which are defaults and not constants.
MODEL = PointsModel(
    base_cost=1,
    late_penalty=1,
    very_late_penalty=2,
    absence_penalty=6,
    late_hours=4.0,
    very_late_hours=1.0,
)


def _record(
    *,
    day: date,
    state: str = "attended",
    hours_before: float | None = 24.0,
    ever_full: bool = True,
    hour: int = 18,
    class_id: int = 1,
) -> AttendanceRecord:
    start = datetime(day.year, day.month, day.day, hour, 30, tzinfo=UTC)
    return AttendanceRecord(
        gym_account_id=1,
        local_date=day,
        wodbuster_class_id=class_id,
        class_name="Cross Training",
        class_type_id=1,
        start_at=start,
        state=state,
        state_changed_at=(None if hours_before is None else start - timedelta(hours=hours_before)),
        reservation_type="Tarifa",
        capacity=14,
        occupancy=14,
        ever_full=ever_full,
    )


def _estimate(*records: AttendanceRecord):
    counted = counted_records(list(records), now=NOW, settle_window_hours=SETTLE)
    return points_estimate(counted, model=MODEL)


def test_a_cancellation_well_ahead_costs_no_penalty() -> None:
    """The gym charges nothing extra above its late tier."""
    result = _estimate(_record(day=date(2026, 9, 1), state="cancelled", hours_before=30.0))

    assert result.penalties == 0
    assert result.early_cancellations == 1


def test_a_late_cancellation_costs_the_late_penalty() -> None:
    result = _estimate(_record(day=date(2026, 9, 1), state="cancelled", hours_before=2.0))

    assert result.penalties == MODEL.late_penalty
    assert result.late_cancellations == 1


def test_a_very_late_cancellation_costs_the_higher_penalty() -> None:
    result = _estimate(_record(day=date(2026, 9, 1), state="cancelled", hours_before=0.5))

    assert result.penalties == MODEL.very_late_penalty
    assert result.very_late_cancellations == 1


def test_an_absence_costs_the_absence_penalty_by_either_path() -> None:
    """At a gym with the attendance control disabled, an absence arrives
    as a post-start removal instead of as a no-show. Both cost the same
    and both stay visible, so an implausible figure is traceable.

    Both instants fall at or after the class start. A "did not train"
    row recorded before it is a removal, however the gym filed it, and
    is charged as one.
    """
    result = _estimate(
        _record(day=date(2026, 9, 1), state="no_show", hours_before=-0.1, class_id=1),
        _record(day=date(2026, 9, 2), state="cancelled", hours_before=-0.5, class_id=2),
    )

    assert result.no_shows == 1
    assert result.removed_after_start == 1
    assert result.penalties == MODEL.absence_penalty * 2
    assert result.disputed_penalties == 0


def test_the_base_cost_is_a_range_and_never_one_number() -> None:
    """The gym overwrites the booking instant on removal, so whether the
    point was ever spent is unknowable."""
    result = _estimate(_record(day=date(2026, 9, 1), state="cancelled", hours_before=30.0))

    assert result.base_cost_low == 0
    assert result.base_cost_high == MODEL.base_cost
    assert result.is_range is True
    assert result.total_low == 0
    assert result.total_high == 1


def test_a_class_that_never_filled_gives_the_base_cost_back() -> None:
    result = _estimate(
        _record(day=date(2026, 9, 1), state="cancelled", hours_before=30.0, ever_full=False)
    )

    assert result.recovered_bookings == 1
    assert result.base_cost_high == 0


def test_recovery_never_cancels_a_penalty() -> None:
    """The gym lists the recovery among the removal rules without saying
    it clears the extra point. ADR-0015 reads it as base cost only."""
    result = _estimate(
        _record(day=date(2026, 9, 1), state="cancelled", hours_before=0.5, ever_full=False)
    )

    assert result.base_cost_high == 0
    assert result.penalties == MODEL.very_late_penalty
    assert result.total_low == MODEL.very_late_penalty


def test_a_class_change_is_priced_and_counted_separately() -> None:
    """CC-051: The gym sees a removal and charges for it, whether or not another
    hour was booked the same day."""
    day = date(2026, 9, 1)
    result = _estimate(
        _record(day=day, state="cancelled", hours_before=2.0, hour=18, class_id=1),
        _record(day=day, state="attended", hour=20, class_id=2),
    )

    assert result.class_changes == 1
    assert result.penalties == MODEL.late_penalty


def test_an_attended_class_is_not_a_loss() -> None:
    """Training costs a point, but a point spent on training is the
    price of the service rather than something lost."""
    result = _estimate(_record(day=date(2026, 9, 1), state="attended"))

    assert result.total_high == 0
    assert result.assumptions == ()


def test_a_removal_with_no_instant_is_reported_not_guessed() -> None:
    result = _estimate(
        _record(day=date(2026, 9, 1), state="cancelled", hours_before=None),
    )

    assert result.unknown_lead == 1
    assert result.penalties == 0


def test_every_priced_figure_carries_its_assumptions() -> None:
    """The structural half of INV-004: a template cannot reach the
    number without also reaching why it is not exact."""
    result = _estimate(_record(day=date(2026, 9, 1), state="cancelled", hours_before=2.0))

    assert result.assumptions != ()
    assert "base_cost_unknown" in result.assumptions
    assert "non_standard_cost" in result.assumptions


def test_the_prices_are_arguments_not_constants() -> None:
    """A second gym will not share Antwork's published tiers."""
    drop = _record(day=date(2026, 9, 1), state="cancelled", hours_before=2.0)
    counted = counted_records([drop], now=NOW, settle_window_hours=SETTLE)

    stricter = points_estimate(
        counted,
        model=PointsModel(
            base_cost=3,
            late_penalty=5,
            very_late_penalty=9,
            absence_penalty=20,
            late_hours=4.0,
            very_late_hours=1.0,
        ),
    )

    assert stricter.penalties == 5
    assert stricter.base_cost_high == 3


# ---------------------------------------------------------------------------
# Weekly average against the preceding range
# ---------------------------------------------------------------------------


def _pace(days: list[date], *, captured: set[date], start: date, end: date):
    counted = counted_records(
        [_record(day=day, class_id=index) for index, day in enumerate(days, start=1)],
        now=NOW,
        settle_window_hours=SETTLE,
    )
    return weekly_average(counted, captured=captured, start=start, end=end)


def test_the_preceding_range_has_the_same_length() -> None:
    start, end = date(2026, 9, 15), date(2026, 9, 21)
    result = _pace([], captured={date(2026, 9, 8)}, start=start, end=end)

    assert result.weeks == result.previous_weeks == 1.0


def test_the_comparison_counts_each_range_on_its_own() -> None:
    start, end = date(2026, 9, 15), date(2026, 9, 21)
    trained = [date(2026, 9, 15), date(2026, 9, 16), date(2026, 9, 9)]
    captured = {start - timedelta(days=offset) for offset in range(1, 8)}

    result = _pace(trained, captured=captured, start=start, end=end)

    assert result.sessions == 2
    assert result.previous_sessions == 1
    assert result.per_week == 2.0
    assert result.change == 1.0


def test_an_unread_preceding_range_is_absent_not_zero() -> None:
    """CC-052: Otherwise the comparison would measure the backfill rather than
    the user (INV-005)."""
    start, end = date(2026, 9, 15), date(2026, 9, 21)
    result = _pace([date(2026, 9, 15)], captured={start}, start=start, end=end)

    assert result.previous_sessions is None
    assert result.previous_per_week is None
    assert result.change is None


# ---------------------------------------------------------------------------
# Per-gym override of the points economy (FR-038)
# ---------------------------------------------------------------------------

DEFAULTS = {
    "base_cost": 1,
    "late_penalty": 1,
    "very_late_penalty": 2,
    "absence_penalty": 6,
    "late_hours": 4.0,
    "very_late_hours": 1.0,
}


def test_without_an_override_the_model_is_the_configured_default() -> None:
    assert PointsModel.resolve(**DEFAULTS) == MODEL


def test_a_gym_override_replaces_only_the_fields_it_names() -> None:
    """A second gym will not share Antwork's published tiers, and may
    differ in one of them rather than all six."""
    model = PointsModel.resolve(**DEFAULTS, override={"absence_penalty": 10})

    assert model.absence_penalty == 10
    assert model.base_cost == MODEL.base_cost
    assert model.late_hours == MODEL.late_hours


def test_a_malformed_override_costs_the_customisation_not_the_page() -> None:
    """The column is operator-supplied JSON, so it is not trusted. A
    bad field falls back rather than raising in the middle of a render.
    """
    model = PointsModel.resolve(
        **DEFAULTS,
        override={
            "base_cost": "free",
            "late_penalty": -3,
            "absence_penalty": True,
            "nonsense": 99,
            "very_late_penalty": 5,
        },
    )

    assert model.base_cost == 1
    assert model.late_penalty == 1
    assert model.absence_penalty == 6
    assert model.very_late_penalty == 5


def test_an_override_reaches_the_estimate() -> None:
    drop = _record(day=date(2026, 9, 1), state="cancelled", hours_before=2.0)
    counted = counted_records([drop], now=NOW, settle_window_hours=SETTLE)

    model = PointsModel.resolve(**DEFAULTS, override={"late_penalty": 7})

    assert points_estimate(counted, model=model).penalties == 7


def test_an_override_that_is_not_an_object_is_treated_as_absent() -> None:
    """``points_model`` is JSONB and holds any JSON value, so a list, a
    string or a number arrives here as readily as an object. Calling
    ``.get`` on one raised and took the whole page down."""
    for junk in ([1, 2, 3], "late_penalty=9", 42, True, ()):
        model = PointsModel.resolve(**DEFAULTS, override=junk)
        assert model == MODEL, junk


def test_an_empty_override_is_the_default() -> None:
    assert PointsModel.resolve(**DEFAULTS, override={}) == MODEL


def test_an_override_can_move_the_tier_boundaries() -> None:
    """The boundaries decide both what is charged and what the page
    calls it, so they have to be overridable like the prices."""
    model = PointsModel.resolve(**DEFAULTS, override={"late_hours": 12, "very_late_hours": 3})

    assert (model.late_hours, model.very_late_hours) == (12.0, 3.0)


def test_an_inverted_tier_pair_falls_back_to_both_defaults() -> None:
    """A very late tier above the late one describes no gym: it empties
    the middle band and labels it with an impossible range. Keeping
    either half of the pair is what produces the incoherence, so both
    go back."""
    model = PointsModel.resolve(**DEFAULTS, override={"late_hours": 1, "very_late_hours": 4})

    assert (model.late_hours, model.very_late_hours) == (4.0, 1.0)


def test_an_inverted_pair_does_not_discard_the_prices_alongside_it() -> None:
    """The boundaries and the prices are independent; one bad pair
    should not cost a valid penalty override."""
    model = PointsModel.resolve(
        **DEFAULTS,
        override={"late_hours": 1, "very_late_hours": 4, "absence_penalty": 9},
    )

    assert model.absence_penalty == 9
    assert (model.late_hours, model.very_late_hours) == (4.0, 1.0)


def test_equal_boundaries_are_accepted() -> None:
    """A gym with a single tier is coherent, if unusual: everything
    under the boundary is the same penalty."""
    model = PointsModel.resolve(**DEFAULTS, override={"late_hours": 2, "very_late_hours": 2})

    assert (model.late_hours, model.very_late_hours) == (2.0, 2.0)


# ---------------------------------------------------------------------------
# Removals the gym filed as "did not train" (ADR-0015 Decision 5, revised)
# ---------------------------------------------------------------------------


def _filed_absent(day: date, *, minutes_before: float = 24.0) -> AttendanceRecord:
    """A row WodBuster put in its "did not train" list."""
    return _record(
        day=day,
        state="no_show",
        hours_before=minutes_before / 60,
        hour=14,
    )


def test_a_removal_the_gym_filed_as_absent_costs_a_range_not_a_number() -> None:
    """The instant says it was a removal under an hour, worth the very
    late tier. The gym's filing says it may have charged the absence
    penalty. Neither is knowable from the payload, so both travel."""
    result = _estimate(_filed_absent(date(2026, 9, 1)))

    assert result.filed_as_absent == 1
    assert result.penalties == MODEL.very_late_penalty
    assert result.disputed_penalties == MODEL.absence_penalty - MODEL.very_late_penalty
    assert result.total_low == 2
    assert result.total_high == 2 + 4 + MODEL.base_cost


def test_the_disputed_half_never_double_charges_the_tier() -> None:
    """The upper end is the absence penalty, not the absence penalty on
    top of the tier already charged."""
    result = _estimate(_filed_absent(date(2026, 9, 1)))

    assert result.penalties + result.disputed_penalties == MODEL.absence_penalty


def test_such_a_removal_carries_its_own_assumption() -> None:
    """INV-004: the reader cannot reach the range without reaching why
    it is a range."""
    result = _estimate(_filed_absent(date(2026, 9, 1)))

    assert "filed_as_absent" in result.assumptions


def test_a_real_absence_is_charged_outright_with_nothing_disputed() -> None:
    """Recorded once the class had started, so nothing contradicts the
    gym and there is nothing to hedge."""
    result = _estimate(_record(day=date(2026, 9, 1), state="no_show", hours_before=-0.5))

    assert result.disputed_penalties == 0
    assert result.penalties == MODEL.absence_penalty
    assert "filed_as_absent" not in result.assumptions


def test_an_ordinary_estimate_disputes_nothing() -> None:
    result = _estimate(_record(day=date(2026, 9, 1), state="cancelled", hours_before=2.0))

    assert result.disputed_penalties == 0
    assert result.filed_as_absent == 0


def test_the_range_contains_both_readings_whatever_their_order() -> None:
    """Nothing stops a gym charging less for an absence than for a very
    late removal. Taking the notice tier as the floor would then print
    a range that excludes the cheaper of the two readings, which is the
    one thing a range exists to prevent."""
    cheap_absence = PointsModel.resolve(
        base_cost=1,
        late_penalty=1,
        very_late_penalty=5,
        absence_penalty=2,
        late_hours=4.0,
        very_late_hours=1.0,
    )
    counted = counted_records(
        [_filed_absent(date(2026, 9, 1))], now=NOW, settle_window_hours=SETTLE
    )

    result = points_estimate(counted, model=cheap_absence)

    assert result.penalties == 2
    assert result.disputed_penalties == 3
    assert result.total_low == 2
    assert result.total_high == 5 + cheap_absence.base_cost


def test_the_usual_ordering_still_puts_the_tier_at_the_floor() -> None:
    """With the gym's published values the cheaper reading is the tier,
    so the floor is what the instant supports."""
    result = _estimate(_filed_absent(date(2026, 9, 1)))

    assert result.penalties == MODEL.very_late_penalty
    assert result.penalties + result.disputed_penalties == MODEL.absence_penalty
