"""Unit tests for the new rule form parsers (US-005 form uplift)."""

from __future__ import annotations

import pytest

from wodbuster_worker.rules.forms import (
    parse_create_rule_form,
    parse_edit_rule_form,
)


def _create_valid() -> dict[str, str]:
    """Minimal valid create submission: Wednesday, 21:30, one preference."""
    return {
        "day_of_week_2": "on",
        "time_slot": "21:30",
        "preference_0_class_type": "WOD",
    }


def _edit_valid() -> dict[str, str]:
    """Minimal valid edit submission."""
    return {
        "day_of_week": "2",
        "time_slot": "21:30",
        "preference_0_class_type": "WOD",
    }


# --- Create form ---------------------------------------------------------


def test_create_minimum_form_parses() -> None:
    result = parse_create_rule_form(_create_valid())
    assert result.is_valid
    assert result.days_of_week == [2]
    assert result.time_slot == "21:30"
    assert len(result.preferences) == 1
    assert result.preferences[0].class_type == "WOD"
    assert result.preferences[0].order_index == 0


def test_create_multi_day_produces_sorted_days_list() -> None:
    form = _create_valid()
    # Add Mon and Fri.
    form["day_of_week_0"] = "on"
    form["day_of_week_4"] = "on"
    result = parse_create_rule_form(form)
    assert result.is_valid
    assert result.days_of_week == [0, 2, 4]  # ascending


def test_create_no_days_selected_is_error() -> None:
    form = _create_valid()
    del form["day_of_week_2"]
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "days_of_week" in result.errors


def test_create_missing_time_slot_is_error() -> None:
    form = _create_valid()
    del form["time_slot"]
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "time_slot" in result.errors


@pytest.mark.parametrize("bad_time", ["25:00", "12:60", "abc", "12", "12:3", "1:00"])
def test_create_malformed_time_slot_is_error(bad_time: str) -> None:
    form = _create_valid()
    form["time_slot"] = bad_time
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "time_slot" in result.errors


def test_create_no_preferences_is_error() -> None:
    form = _create_valid()
    del form["preference_0_class_type"]
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "preferences" in result.errors


def test_create_empty_preference_slots_compact_order_index() -> None:
    form = _create_valid()
    # Slot 0 filled (WOD), leave slot 1 blank, fill slot 2.
    form["preference_2_class_type"] = "Halterofilia"
    result = parse_create_rule_form(form)
    assert result.is_valid
    assert [p.class_type for p in result.preferences] == ["WOD", "Halterofilia"]
    # order_index compacts around the blank slot.
    assert [p.order_index for p in result.preferences] == [0, 1]


def test_create_whitespace_only_preference_is_treated_as_empty() -> None:
    form = _create_valid()
    form["preference_0_class_type"] = "   "
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "preferences" in result.errors


def test_create_all_five_preferences() -> None:
    form = _create_valid()
    for i in range(5):
        form[f"preference_{i}_class_type"] = f"Type{i}"
    result = parse_create_rule_form(form)
    assert result.is_valid
    assert len(result.preferences) == 5
    assert [p.order_index for p in result.preferences] == [0, 1, 2, 3, 4]


@pytest.mark.parametrize("checkbox_value", ["on", "ON", "true", "1", "yes"])
def test_create_accepts_multiple_truthy_checkbox_values(checkbox_value: str) -> None:
    # Browsers send ``on`` but tests may synthesise other strings.
    form = {
        "day_of_week_2": checkbox_value,
        "time_slot": "21:30",
        "preference_0_class_type": "WOD",
    }
    result = parse_create_rule_form(form)
    assert result.is_valid


# --- Edit form -----------------------------------------------------------


def test_edit_minimum_form_parses() -> None:
    result = parse_edit_rule_form(_edit_valid())
    assert result.is_valid
    assert result.day_of_week == 2
    assert result.time_slot == "21:30"
    assert len(result.preferences) == 1


def test_edit_missing_day_of_week_is_error() -> None:
    form = _edit_valid()
    del form["day_of_week"]
    result = parse_edit_rule_form(form)
    assert not result.is_valid
    assert "day_of_week" in result.errors


@pytest.mark.parametrize("bad_day", ["-1", "7", "999", "abc"])
def test_edit_out_of_range_day_is_error(bad_day: str) -> None:
    form = _edit_valid()
    form["day_of_week"] = bad_day
    result = parse_edit_rule_form(form)
    assert not result.is_valid
    assert "day_of_week" in result.errors


def test_edit_shares_preference_and_time_validation() -> None:
    form = _edit_valid()
    del form["preference_0_class_type"]
    del form["time_slot"]
    result = parse_edit_rule_form(form)
    assert not result.is_valid
    assert "preferences" in result.errors
    assert "time_slot" in result.errors
