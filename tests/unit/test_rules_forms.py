"""Unit tests for :func:`parse_rule_form` (US5.T3)."""

from __future__ import annotations

import pytest

from wodbuster_worker.rules.forms import parse_rule_form


def _valid_form() -> dict[str, str]:
    """A minimal complete form: Wed 48h before class, one preference."""
    return {
        "day_of_week": "2",
        "window_offset_hours": "48",
        "preference_0_class_type": "WOD",
        "preference_0_time_slot": "21:30",
    }


def test_valid_minimum_form_parses() -> None:
    result = parse_rule_form(_valid_form())
    assert result.is_valid
    assert result.errors == {}
    assert result.day_of_week == 2
    assert result.window_offset_hours == 48
    assert len(result.preferences) == 1
    assert result.preferences[0].class_type == "WOD"
    assert result.preferences[0].target_time_slot == "21:30"
    assert result.preferences[0].order_index == 0


def test_missing_day_of_week_is_error() -> None:
    form = _valid_form()
    del form["day_of_week"]
    result = parse_rule_form(form)
    assert not result.is_valid
    assert "day_of_week" in result.errors


@pytest.mark.parametrize("bad", ["-1", "7", "999", "abc"])
def test_out_of_range_day_of_week_is_error(bad: str) -> None:
    form = _valid_form()
    form["day_of_week"] = bad
    result = parse_rule_form(form)
    assert not result.is_valid
    assert "day_of_week" in result.errors


def test_negative_offset_is_error() -> None:
    form = _valid_form()
    form["window_offset_hours"] = "-1"
    result = parse_rule_form(form)
    assert not result.is_valid
    assert "window_offset_hours" in result.errors


def test_offset_over_one_week_is_error() -> None:
    form = _valid_form()
    form["window_offset_hours"] = "200"
    result = parse_rule_form(form)
    assert not result.is_valid
    assert "window_offset_hours" in result.errors


def test_empty_preference_list_is_error() -> None:
    form = {
        "day_of_week": "2",
        "window_offset_hours": "48",
    }
    result = parse_rule_form(form)
    assert not result.is_valid
    assert result.errors.get("preferences") == "At least one preference is required."


def test_multiple_preferences_get_sequential_order_index() -> None:
    form = _valid_form()
    form["preference_1_class_type"] = "WOD"
    form["preference_1_time_slot"] = "22:30"
    form["preference_2_class_type"] = "Halterofilia"
    form["preference_2_time_slot"] = "23:00"

    result = parse_rule_form(form)

    assert result.is_valid
    assert len(result.preferences) == 3
    assert [p.order_index for p in result.preferences] == [0, 1, 2]


def test_empty_preference_slot_is_silently_skipped() -> None:
    form = _valid_form()
    # Slot 1 completely blank.
    form["preference_2_class_type"] = "Halterofilia"
    form["preference_2_time_slot"] = "23:00"

    result = parse_rule_form(form)

    assert result.is_valid
    assert len(result.preferences) == 2
    assert result.preferences[1].class_type == "Halterofilia"
    # order_index compacts around blank rows.
    assert [p.order_index for p in result.preferences] == [0, 1]


def test_partial_preference_row_is_error() -> None:
    form = _valid_form()
    # Slot 1 has a class type but no time slot.
    form["preference_1_class_type"] = "Halterofilia"
    result = parse_rule_form(form)
    assert not result.is_valid
    assert "preference_1_time_slot" in result.errors


def test_partial_row_time_slot_only_is_error() -> None:
    form = _valid_form()
    form["preference_1_time_slot"] = "22:30"
    result = parse_rule_form(form)
    assert not result.is_valid
    assert "preference_1_class_type" in result.errors


@pytest.mark.parametrize("bad", ["25:00", "12:60", "abc", "12", "12:3", "1:00"])
def test_malformed_time_slot_is_error(bad: str) -> None:
    form = _valid_form()
    form["preference_0_time_slot"] = bad
    result = parse_rule_form(form)
    assert not result.is_valid
    assert "preference_0_time_slot" in result.errors


def test_whitespace_only_class_type_is_treated_as_empty() -> None:
    # Whitespace should not sneak past the required-field check.
    form = _valid_form()
    form["preference_0_class_type"] = "   "
    form["preference_0_time_slot"] = "21:30"
    result = parse_rule_form(form)
    # Class-type-required error on slot 0.
    assert "preference_0_class_type" in result.errors


def test_boundary_offset_zero_is_valid() -> None:
    form = _valid_form()
    form["window_offset_hours"] = "0"
    result = parse_rule_form(form)
    assert result.is_valid


def test_boundary_offset_168_is_valid() -> None:
    form = _valid_form()
    form["window_offset_hours"] = "168"
    result = parse_rule_form(form)
    assert result.is_valid
