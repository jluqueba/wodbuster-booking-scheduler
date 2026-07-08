"""Rule form parsing and validation (US5.3, US5.T3).

The web layer submits a rule as a flat form:

- ``day_of_week`` — integer 0..6 (Mon..Sun, Python ``datetime.weekday()``)
- ``window_offset_hours`` — non-negative integer
- ``preferences[n].class_type`` — non-empty label per non-empty row
- ``preferences[n].target_time_slot`` — ``HH:MM`` per non-empty row

Rows are indexed by position; empty rows (both fields blank) are
silently dropped. At least one non-empty row is required.

This module is deliberately independent of Starlette / FastAPI so the
unit tests can drive it with plain dicts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import time

_MAX_WINDOW_OFFSET_HOURS = 168  # one week; anything longer is a data-entry bug
_MAX_PREFERENCE_SLOTS = 5


@dataclass(frozen=True)
class PreferenceInput:
    """Parsed, validated preference row.

    ``order_index`` is assigned by :func:`parse_rule_form` based on the
    row's position among *non-empty* rows, so a form that leaves slot
    1 empty and fills slots 0 and 2 still produces ``order_index=0, 1``.
    """

    order_index: int
    class_type: str
    target_time_slot: str  # ``HH:MM``


@dataclass
class RuleFormResult:
    """Outcome of :func:`parse_rule_form`.

    On success, ``errors`` is empty and the other fields are populated.
    On failure, ``errors`` maps field name → message; the template
    renders these next to the offending input.
    """

    day_of_week: int | None = None
    window_offset_hours: int | None = None
    preferences: list[PreferenceInput] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def parse_rule_form(form: dict[str, str]) -> RuleFormResult:
    """Parse and validate a submitted rule form.

    ``form`` is the flat form dict. Preference rows use the naming
    convention ``preference_{index}_class_type`` /
    ``preference_{index}_time_slot`` for ``index in 0..4``. Empty
    (both blank) rows are ignored.
    """
    result = RuleFormResult()

    result.day_of_week = _parse_day_of_week(form.get("day_of_week"), result.errors)
    result.window_offset_hours = _parse_offset(
        form.get("window_offset_hours"), result.errors
    )
    result.preferences = _parse_preferences(form, result.errors)

    return result


def _parse_day_of_week(raw: str | None, errors: dict[str, str]) -> int | None:
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


def _parse_offset(raw: str | None, errors: dict[str, str]) -> int | None:
    if raw is None or raw == "":
        errors["window_offset_hours"] = "Enter the window offset in hours."
        return None
    try:
        value = int(raw)
    except ValueError:
        errors["window_offset_hours"] = "Window offset must be an integer."
        return None
    if value < 0:
        errors["window_offset_hours"] = "Window offset cannot be negative."
        return None
    if value > _MAX_WINDOW_OFFSET_HOURS:
        errors["window_offset_hours"] = (
            f"Window offset cannot exceed {_MAX_WINDOW_OFFSET_HOURS} hours."
        )
        return None
    return value


def _parse_preferences(
    form: dict[str, str], errors: dict[str, str]
) -> list[PreferenceInput]:
    parsed: list[PreferenceInput] = []
    next_index = 0
    for slot in range(_MAX_PREFERENCE_SLOTS):
        class_type = (form.get(f"preference_{slot}_class_type") or "").strip()
        time_slot = (form.get(f"preference_{slot}_time_slot") or "").strip()

        if not class_type and not time_slot:
            # Empty row — silently skip. This lets operators leave
            # unused fallback slots blank without a validation error.
            continue

        # Partial row: one field filled, the other empty. That is a
        # data-entry mistake, not an intentional omission.
        if not class_type:
            errors[f"preference_{slot}_class_type"] = (
                "Class type is required when a time slot is set."
            )
            continue
        if not time_slot:
            errors[f"preference_{slot}_time_slot"] = (
                "Time slot is required when a class type is set."
            )
            continue

        if not _valid_time_slot(time_slot):
            errors[f"preference_{slot}_time_slot"] = (
                "Time slot must be in HH:MM format."
            )
            continue

        parsed.append(
            PreferenceInput(
                order_index=next_index,
                class_type=class_type,
                target_time_slot=time_slot,
            )
        )
        next_index += 1

    if not parsed and "preferences" not in errors:
        # At least one preference is required. The error is not tied to
        # a specific slot because a completely blank preference list is
        # a form-level omission.
        errors["preferences"] = "At least one preference is required."

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
    "PreferenceInput",
    "RuleFormResult",
    "parse_rule_form",
]
