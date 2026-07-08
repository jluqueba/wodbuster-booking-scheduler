"""Unit tests for the class-list extractor (US-005 form uplift).

The pure :func:`extract_available_classes` is the sortable core.
The wrapper :func:`fetch_available_classes` is covered indirectly by
the routes' component tests via a fake WodBuster client.
"""

from __future__ import annotations

from wodbuster_worker.rules.classes import (
    AvailableClasses,
    extract_available_classes,
)


def test_extract_dedupes_class_types_and_time_slots() -> None:
    items = [
        {"NombreE": "Cross Training", "Hora": "07:30:00"},
        {"NombreE": "Cross Training", "Hora": "18:30:00"},  # dup class-type
        {"NombreE": "WOD", "Hora": "18:30:00"},  # dup time
        {"NombreE": "Halterofilia", "Hora": "21:30:00"},
    ]
    result = extract_available_classes(items)
    assert isinstance(result, AvailableClasses)
    assert result.class_types == ["Cross Training", "Halterofilia", "WOD"]  # sorted
    assert result.time_slots == ["07:30", "18:30", "21:30"]  # sorted


def test_extract_strips_seconds_from_time() -> None:
    items = [{"NombreE": "WOD", "Hora": "07:30:00"}]
    result = extract_available_classes(items)
    assert result.time_slots == ["07:30"]  # not "07:30:00"


def test_extract_skips_items_without_required_fields() -> None:
    items = [
        {"NombreE": "WOD", "Hora": "07:30:00"},
        {"NombreE": "WOD"},  # missing hora
        {"Hora": "18:30:00"},  # missing nombre
        {"NombreE": "", "Hora": ""},  # empty
        {"NombreE": "  ", "Hora": "20:30:00"},  # whitespace-only nombre
        "not a dict",  # wrong type
    ]
    result = extract_available_classes(items)  # type: ignore[arg-type]
    # Only the well-formed rows contribute.
    assert result.class_types == ["WOD"]
    assert "18:30" in result.time_slots
    assert "20:30" in result.time_slots  # nombre skipped, but hora still counts
    assert "07:30" in result.time_slots


def test_extract_empty_input_returns_empty_lists() -> None:
    result = extract_available_classes([])
    assert result.class_types == []
    assert result.time_slots == []
    assert result.is_empty


def test_extract_ignores_malformed_time_strings() -> None:
    items = [
        {"NombreE": "WOD", "Hora": "07:30:00"},  # good
        {"NombreE": "WOD", "Hora": "0730"},  # no colon
        {"NombreE": "WOD", "Hora": "7:30"},  # too short
    ]
    result = extract_available_classes(items)
    assert result.time_slots == ["07:30"]


def test_extract_realistic_payload() -> None:
    # Sample based on the operator's redacted debug output. Real gyms
    # have ~15-30 slots per day; sampled here to keep the test compact.
    items = [
        {"NombreE": "Cross Training", "IdE": 1, "Hora": "07:30:00", "Id": 12},
        {"NombreE": "Cross Training", "IdE": 1, "Hora": "08:30:00", "Id": 13},
        {"NombreE": "WOD", "IdE": 2, "Hora": "18:30:00", "Id": 14},
        {"NombreE": "WOD", "IdE": 2, "Hora": "19:30:00", "Id": 15},
        {"NombreE": "WOD", "IdE": 2, "Hora": "20:30:00", "Id": 16},
        {"NombreE": "WOD", "IdE": 2, "Hora": "21:30:00", "Id": 17},
        {"NombreE": "Halterofilia", "IdE": 3, "Hora": "18:30:00", "Id": 18},
    ]
    result = extract_available_classes(items)
    assert result.class_types == ["Cross Training", "Halterofilia", "WOD"]
    assert result.time_slots == ["07:30", "08:30", "18:30", "19:30", "20:30", "21:30"]
