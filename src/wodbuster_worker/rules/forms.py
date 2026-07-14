"""Rule form parsing and validation (rule model v2).

Two form shapes — create and edit — because they differ on the
day-of-week field only. Create takes ``day_of_week_{n}`` checkboxes
for multi-day fan-out (submitting Mon+Wed+Fri creates three rules
under the hood). Edit takes a single ``day_of_week`` value so the
operator can retarget one rule without triggering the fan-out.

Both shapes share:

- ``class_type`` — primary class type string.
- ``class_time`` — primary class start time (``HH:MM``).
- ``booking_opens_days_before`` — how many days before the class the
  reservation window opens.
- ``booking_opens_at`` — clock time the window opens on the trigger
  day (``HH:MM``).
- ``second_shot_class_type`` / ``second_shot_class_time`` — optional
  alternative pair. Both must be present or both absent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

_TRUTHY = {"on", "true", "1", "yes"}


@dataclass
class CreateRuleFormResult:
    """Outcome of :func:`parse_create_rule_form`."""

    days_of_week: list[int] = field(default_factory=list)
    class_type: str | None = None
    class_time: str | None = None
    booking_opens_days_before: int | None = None
    booking_opens_at: str | None = None
    second_shot_class_type: str | None = None
    second_shot_class_time: str | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass
class EditRuleFormResult:
    """Outcome of :func:`parse_edit_rule_form`. Single day; no fan-out."""

    day_of_week: int | None = None
    class_type: str | None = None
    class_time: str | None = None
    booking_opens_days_before: int | None = None
    booking_opens_at: str | None = None
    second_shot_class_type: str | None = None
    second_shot_class_time: str | None = None
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def parse_create_rule_form(form: dict[str, str]) -> CreateRuleFormResult:
    """Parse the create form."""
    result = CreateRuleFormResult()
    result.days_of_week = _parse_days_multi(form, result.errors)
    _parse_shared_fields(form, result)
    return result


def parse_edit_rule_form(form: dict[str, str]) -> EditRuleFormResult:
    """Parse the edit form (single day, no fan-out)."""
    result = EditRuleFormResult()
    result.day_of_week = _parse_day_single(form, result.errors)
    _parse_shared_fields(form, result)
    return result


def _parse_shared_fields(
    form: dict[str, str], result: CreateRuleFormResult | EditRuleFormResult
) -> None:
    result.class_type = _parse_required_str(form, "class_type", "Pick a class type.", result.errors)
    result.class_time = _parse_required_time(
        form, "class_time", "Pick a class time.", result.errors
    )
    result.booking_opens_days_before = _parse_days_before(form, result.errors)
    result.booking_opens_at = _parse_required_time(
        form,
        "booking_opens_at",
        "Pick the time the booking window opens.",
        result.errors,
    )
    (
        result.second_shot_class_type,
        result.second_shot_class_time,
    ) = _parse_second_shot(form, result.errors)


def _parse_days_multi(form: dict[str, str], errors: dict[str, str]) -> list[int]:
    days: list[int] = []
    for day in range(7):
        value = form.get(f"day_of_week_{day}")
        if value is not None and value.lower() in _TRUTHY:
            days.append(day)
    if not days:
        errors["days_of_week"] = "Select at least one day of the week."
    return days


def _parse_day_single(form: dict[str, str], errors: dict[str, str]) -> int | None:
    raw = form.get("day_of_week")
    if raw is None or raw == "":
        errors["day_of_week"] = "Select a day of the week."
        return None
    try:
        value = int(raw)
    except ValueError:
        errors["day_of_week"] = "Day of week must be an integer 0-6."
        return None
    if not 0 <= value <= 6:
        errors["day_of_week"] = "Day of week must be between 0 (Mon) and 6 (Sun)."
        return None
    return value


def _parse_required_str(
    form: dict[str, str], field_name: str, message: str, errors: dict[str, str]
) -> str | None:
    raw = (form.get(field_name) or "").strip()
    if not raw:
        errors[field_name] = message
        return None
    return raw


def _parse_required_time(
    form: dict[str, str], field_name: str, message: str, errors: dict[str, str]
) -> str | None:
    raw = (form.get(field_name) or "").strip()
    if not raw:
        errors[field_name] = message
        return None
    if not _valid_time_slot(raw):
        errors[field_name] = f"{field_name} must be in HH:MM format."
        return None
    return raw


def _parse_days_before(form: dict[str, str], errors: dict[str, str]) -> int | None:
    raw = (form.get("booking_opens_days_before") or "").strip()
    if not raw:
        errors["booking_opens_days_before"] = "How many days before class does the window open?"
        return None
    try:
        value = int(raw)
    except ValueError:
        errors["booking_opens_days_before"] = "Must be a whole number."
        return None
    if value < 0 or value > 14:
        errors["booking_opens_days_before"] = "Must be between 0 and 14 days."
        return None
    return value


def _parse_second_shot(
    form: dict[str, str], errors: dict[str, str]
) -> tuple[str | None, str | None]:
    """Parse the optional second-shot pair.

    Both fields must be present together or absent together. A single
    filled field is treated as a validation error rather than silently
    dropped.
    """
    raw_type = (form.get("second_shot_class_type") or "").strip()
    raw_time = (form.get("second_shot_class_time") or "").strip()
    if not raw_type and not raw_time:
        return None, None
    if raw_type and not raw_time:
        errors["second_shot_class_time"] = (
            "Pick a time for the second shot, or clear the class type."
        )
        return raw_type, None
    if raw_time and not raw_type:
        errors["second_shot_class_type"] = (
            "Pick a class type for the second shot, or clear the time."
        )
        return None, raw_time
    if not _valid_time_slot(raw_time):
        errors["second_shot_class_time"] = "Time must be in HH:MM format."
        return raw_type, None
    return raw_type, raw_time


def _valid_time_slot(value: str) -> bool:
    """Accept ``HH:MM`` in 24h clock; reject anything else."""
    if len(value) != 5 or value[2] != ":":
        return False
    try:
        time(hour=int(value[:2]), minute=int(value[3:]))
    except ValueError:
        return False
    return True


__all__ = [
    "CreateRuleFormResult",
    "EditRuleFormResult",
    "parse_create_rule_form",
    "parse_edit_rule_form",
]
