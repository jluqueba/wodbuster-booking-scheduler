"""Unit tests for the rule form parsers (rule model v2)."""

from __future__ import annotations

import pytest

from wodbuster_worker.rules.forms import (
    parse_create_rule_form,
    parse_edit_rule_form,
)


def _create_valid() -> dict[str, str]:
    """Minimal valid create submission: Wednesday, WOD 21:30, opens 2d before at 21:30."""
    return {
        "day_of_week_2": "on",
        "class_type": "WOD",
        "class_time": "21:30",
        "booking_opens_days_before": "2",
        "booking_opens_at": "21:30",
    }


def _edit_valid() -> dict[str, str]:
    """Minimal valid edit submission."""
    return {
        "day_of_week": "2",
        "class_type": "WOD",
        "class_time": "21:30",
        "booking_opens_days_before": "2",
        "booking_opens_at": "21:30",
    }


# --- Create form ---------------------------------------------------------


def test_create_minimum_form_parses() -> None:
    result = parse_create_rule_form(_create_valid())
    assert result.is_valid
    assert result.days_of_week == [2]
    assert result.class_type == "WOD"
    assert result.class_time == "21:30"
    assert result.booking_opens_days_before == 2
    assert result.booking_opens_at == "21:30"
    assert result.second_shot_class_type is None
    assert result.second_shot_class_time is None


def test_create_multi_day_produces_sorted_days_list() -> None:
    form = _create_valid()
    form["day_of_week_0"] = "on"
    form["day_of_week_4"] = "on"
    result = parse_create_rule_form(form)
    assert result.is_valid
    assert result.days_of_week == [0, 2, 4]


def test_create_no_days_selected_is_error() -> None:
    form = _create_valid()
    del form["day_of_week_2"]
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "days_of_week" in result.errors


def test_create_missing_class_type_is_error() -> None:
    form = _create_valid()
    del form["class_type"]
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "class_type" in result.errors


def test_create_missing_class_time_is_error() -> None:
    form = _create_valid()
    del form["class_time"]
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "class_time" in result.errors


@pytest.mark.parametrize("bad_time", ["25:00", "12:60", "abc", "12", "12:3", "1:00"])
def test_create_malformed_class_time_is_error(bad_time: str) -> None:
    form = _create_valid()
    form["class_time"] = bad_time
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "class_time" in result.errors


def test_create_missing_booking_opens_days_before_is_error() -> None:
    form = _create_valid()
    del form["booking_opens_days_before"]
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "booking_opens_days_before" in result.errors


@pytest.mark.parametrize("bad_value", ["-1", "15", "abc"])
def test_create_out_of_range_days_before_is_error(bad_value: str) -> None:
    form = _create_valid()
    form["booking_opens_days_before"] = bad_value
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "booking_opens_days_before" in result.errors


def test_create_missing_booking_opens_at_is_error() -> None:
    form = _create_valid()
    del form["booking_opens_at"]
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "booking_opens_at" in result.errors


def test_create_with_second_shot_pair() -> None:
    form = _create_valid()
    form["second_shot_class_type"] = "Halterofilia"
    form["second_shot_class_time"] = "20:30"
    result = parse_create_rule_form(form)
    assert result.is_valid
    assert result.second_shot_class_type == "Halterofilia"
    assert result.second_shot_class_time == "20:30"


def test_create_second_shot_type_without_time_is_error() -> None:
    form = _create_valid()
    form["second_shot_class_type"] = "Halterofilia"
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "second_shot_class_time" in result.errors


def test_create_second_shot_time_without_type_is_error() -> None:
    form = _create_valid()
    form["second_shot_class_time"] = "20:30"
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "second_shot_class_type" in result.errors


def test_create_second_shot_malformed_time_is_error() -> None:
    form = _create_valid()
    form["second_shot_class_type"] = "Halterofilia"
    form["second_shot_class_time"] = "not-a-time"
    result = parse_create_rule_form(form)
    assert not result.is_valid
    assert "second_shot_class_time" in result.errors


def test_create_second_shot_whitespace_only_treated_as_empty() -> None:
    form = _create_valid()
    form["second_shot_class_type"] = "   "
    form["second_shot_class_time"] = "   "
    result = parse_create_rule_form(form)
    assert result.is_valid
    assert result.second_shot_class_type is None
    assert result.second_shot_class_time is None


@pytest.mark.parametrize("checkbox_value", ["on", "ON", "true", "1", "yes"])
def test_create_accepts_multiple_truthy_checkbox_values(checkbox_value: str) -> None:
    form = _create_valid()
    form["day_of_week_2"] = checkbox_value
    result = parse_create_rule_form(form)
    assert result.is_valid


# --- Edit form -----------------------------------------------------------


def test_edit_minimum_form_parses() -> None:
    result = parse_edit_rule_form(_edit_valid())
    assert result.is_valid
    assert result.day_of_week == 2
    assert result.class_type == "WOD"
    assert result.class_time == "21:30"
    assert result.booking_opens_days_before == 2
    assert result.booking_opens_at == "21:30"


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


def test_edit_shares_shared_field_validation() -> None:
    form = _edit_valid()
    del form["class_type"]
    del form["class_time"]
    result = parse_edit_rule_form(form)
    assert not result.is_valid
    assert "class_type" in result.errors
    assert "class_time" in result.errors


def test_edit_with_second_shot_pair() -> None:
    form = _edit_valid()
    form["second_shot_class_type"] = "Halterofilia"
    form["second_shot_class_time"] = "20:30"
    result = parse_edit_rule_form(form)
    assert result.is_valid
    assert result.second_shot_class_type == "Halterofilia"
    assert result.second_shot_class_time == "20:30"
