"""Rule form parsing and validation (US-005 form uplift).

Two form shapes — create and edit — because they differ on the
day-of-week field only. Create takes ``day_of_week_{n}`` checkboxes
for multi-day fan-out (submitting Mon+Wed+Fri creates three rules
under the hood). Edit takes a single ``day_of_week`` value so the
operator can retarget one rule without triggering the fan-out.

Both shapes share:

- ``time_slot`` — the class start time (``HH:MM``). One value per
  rule; ``target_time_slot`` on every stored preference is
  denormalised from this to avoid a schema migration.
- ``preferences`` — ordered list of class-type strings. At least one
  is required. Empty slots between filled ones are silently skipped
  (order_index compacts around them).

The window offset is not a form field: it comes from the global
``settings.wodbuster_booking_lead_hours`` (default 48h). The rule form
therefore stays "day + time + class types", matching the "super
simple" ask.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

_MAX_PREFERENCE_SLOTS = 5
_TRUTHY = {"on", "true", "1", "yes"}


@dataclass(frozen=True)
class PreferenceInput:
    """Parsed preference row — class type only.

    ``order_index`` is assigned by the parser based on the row's
    position among non-empty rows, not the submitted slot index.
    """

    order_index: int
    class_type: str


@dataclass
class CreateRuleFormResult:
    """Outcome of :func:`parse_create_rule_form`."""

    days_of_week: list[int] = field(default_factory=list)
    time_slot: str | None = None
    preferences: list[PreferenceInput] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


@dataclass
class EditRuleFormResult:
    """Outcome of :func:`parse_edit_rule_form`. Single day; no fan-out."""

    day_of_week: int | None = None
    time_slot: str | None = None
    preferences: list[PreferenceInput] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def parse_create_rule_form(form: dict[str, str]) -> CreateRuleFormResult:
    """Parse the create form: multi-day checkboxes + time + class types."""
    result = CreateRuleFormResult()
    result.days_of_week = _parse_days_multi(form, result.errors)
    result.time_slot = _parse_time_slot(form, result.errors)
    result.preferences = _parse_preferences(form, result.errors)
    return result


def parse_edit_rule_form(form: dict[str, str]) -> EditRuleFormResult:
    """Parse the edit form: single day + time + class types."""
    result = EditRuleFormResult()
    result.day_of_week = _parse_day_single(form, result.errors)
    result.time_slot = _parse_time_slot(form, result.errors)
    result.preferences = _parse_preferences(form, result.errors)
    return result


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


def _parse_time_slot(form: dict[str, str], errors: dict[str, str]) -> str | None:
    raw = (form.get("time_slot") or "").strip()
    if not raw:
        errors["time_slot"] = "Pick a time slot."
        return None
    if not _valid_time_slot(raw):
        errors["time_slot"] = "Time slot must be in HH:MM format."
        return None
    return raw


def _parse_preferences(
    form: dict[str, str], errors: dict[str, str]
) -> list[PreferenceInput]:
    parsed: list[PreferenceInput] = []
    next_index = 0
    for slot in range(_MAX_PREFERENCE_SLOTS):
        class_type = (form.get(f"preference_{slot}_class_type") or "").strip()
        if not class_type:
            continue
        parsed.append(PreferenceInput(order_index=next_index, class_type=class_type))
        next_index += 1
    if not parsed:
        errors["preferences"] = "Add at least one class-type preference."
    return parsed


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
    "PreferenceInput",
    "parse_create_rule_form",
    "parse_edit_rule_form",
]
