"""Unit tests for the shared override arithmetic (T-BDO-003, ADR-0012).

The DST cases are the reason this module exists: an override is keyed on
an operator-local calendar day while every slot the scheduler works with
is UTC, so a wrong conversion books the right class on the wrong day and
fails silently.

``WORKER_TIMEZONE`` is set explicitly per test rather than pinned to UTC:
the conversions under test are identities under UTC, which would make
every assertion vacuous.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from wodbuster_worker.booking.overrides import (
    DEFAULT_EDIT_MARGIN_S,
    OverridePlan,
    edit_cutoff_for,
    effective_slot_for,
    is_editable,
    local_date_for_slot,
    window_open_for,
)
from wodbuster_worker.persistence.models import SchedulerRule
from wodbuster_worker.scheduler.rule_jobs import DEFAULT_PREWARM_LEAD_S


@pytest.fixture
def madrid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")


def _rule(
    rule_id: int = 7,
    *,
    day_of_week: int = 2,
    class_type: str = "WOD",
    class_time: str = "18:30",
    booking_opens_days_before: int = 2,
    booking_opens_at: str = "21:30",
) -> SchedulerRule:
    rule = SchedulerRule(
        gym_account_id=1,
        day_of_week=day_of_week,
        class_type=class_type,
        class_time=class_time,
        booking_opens_days_before=booking_opens_days_before,
        booking_opens_at=booking_opens_at,
        active=True,
    )
    rule.id = rule_id
    return rule


def _plan(
    *,
    class_type: str | None = None,
    class_time: str | None = None,
    skip_day: bool = False,
    suppress_second_shot: bool = False,
) -> OverridePlan:
    return OverridePlan(
        override_id=1,
        rule_id=7,
        target_date=date(2026, 5, 6),
        class_type=class_type,
        class_time=class_time,
        skip_day=skip_day,
        validated=True,
        suppress_second_shot=suppress_second_shot,
    )


# ---------------------------------------------------------------------------
# edit_cutoff_for / is_editable
# ---------------------------------------------------------------------------


def test_edit_cutoff_is_window_open_minus_prewarm_and_margin(madrid: None) -> None:
    rule = _rule(booking_opens_days_before=2, booking_opens_at="21:30")
    target = date(2026, 5, 6)  # Wednesday

    cutoff = edit_cutoff_for(rule, target)

    expected = window_open_for(rule, target) - timedelta(
        seconds=DEFAULT_PREWARM_LEAD_S + DEFAULT_EDIT_MARGIN_S
    )
    assert cutoff == expected


def test_window_open_is_days_before_at_opens_at_local(madrid: None) -> None:
    rule = _rule(booking_opens_days_before=2, booking_opens_at="21:30")

    # Class on Wed 2026-05-06, window opens Mon 2026-05-04 21:30 Madrid
    # (CEST, UTC+2) = 19:30 UTC.
    assert window_open_for(rule, date(2026, 5, 6)) == datetime(2026, 5, 4, 19, 30, tzinfo=UTC)


def test_is_editable_flips_at_the_cutoff(madrid: None) -> None:
    rule = _rule()
    target = date(2026, 5, 6)
    cutoff = edit_cutoff_for(rule, target)

    assert is_editable(rule, target, cutoff - timedelta(seconds=1)) is True
    assert is_editable(rule, target, cutoff + timedelta(seconds=1)) is False


def test_is_editable_rejects_naive_now(madrid: None) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        is_editable(_rule(), date(2026, 5, 6), datetime(2026, 5, 4, 12, 0))


# ---------------------------------------------------------------------------
# local_date_for_slot / effective_slot_for
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "target_date",
    [
        date(2026, 3, 28),  # day before Madrid spring-forward
        date(2026, 3, 29),  # spring-forward (02:00 -> 03:00)
        date(2026, 3, 30),
        date(2026, 10, 24),
        date(2026, 10, 25),  # autumn-back (03:00 -> 02:00)
        date(2026, 10, 26),
    ],
)
def test_slot_round_trips_across_dst_transitions(madrid: None, target_date: date) -> None:
    rule = _rule()

    slot = effective_slot_for(rule, target_date, "07:00")

    assert slot.tzinfo is UTC
    assert local_date_for_slot(slot) == target_date


def test_spring_forward_shifts_the_utc_offset(madrid: None) -> None:
    """The same wall time maps to a different UTC instant across the change."""
    rule = _rule()

    before = effective_slot_for(rule, date(2026, 3, 28), "07:00")
    after = effective_slot_for(rule, date(2026, 3, 30), "07:00")

    assert before == datetime(2026, 3, 28, 6, 0, tzinfo=UTC)  # CET, UTC+1
    assert after == datetime(2026, 3, 30, 5, 0, tzinfo=UTC)  # CEST, UTC+2


def test_late_evening_class_west_of_utc_keeps_its_local_date(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 21:00 local class lands on the next UTC day, but the same local day."""
    monkeypatch.setenv("WORKER_TIMEZONE", "America/New_York")
    rule = _rule()

    slot = effective_slot_for(rule, date(2026, 1, 15), "21:00")

    assert slot == datetime(2026, 1, 16, 2, 0, tzinfo=UTC)
    assert local_date_for_slot(slot) == date(2026, 1, 15)


def test_local_date_for_slot_rejects_naive_input(madrid: None) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        local_date_for_slot(datetime(2026, 5, 6, 16, 30))


# ---------------------------------------------------------------------------
# OverridePlan
# ---------------------------------------------------------------------------


def test_plan_falls_back_to_the_rule_when_both_values_are_absent() -> None:
    rule = _rule(class_type="WOD", class_time="18:30")
    plan = _plan(class_type=None, class_time=None)

    assert plan.effective_class_type(rule) == "WOD"
    assert plan.effective_class_time(rule) == "18:30"


def test_plan_overrides_only_the_type() -> None:
    rule = _rule(class_type="WOD", class_time="18:30")
    plan = _plan(class_type="Endurance", class_time=None)

    assert plan.effective_class_type(rule) == "Endurance"
    assert plan.effective_class_time(rule) == "18:30"


def test_plan_overrides_only_the_time() -> None:
    rule = _rule(class_type="WOD", class_time="18:30")
    plan = _plan(class_type=None, class_time="07:00")

    assert plan.effective_class_type(rule) == "WOD"
    assert plan.effective_class_time(rule) == "07:00"


def test_plan_overrides_both() -> None:
    rule = _rule(class_type="WOD", class_time="18:30")
    plan = _plan(class_type="Endurance", class_time="07:00")

    assert plan.effective_class_type(rule) == "Endurance"
    assert plan.effective_class_time(rule) == "07:00"


def test_plan_is_frozen() -> None:
    plan = _plan()

    with pytest.raises(AttributeError):
        plan.skip_day = True  # type: ignore[misc]
