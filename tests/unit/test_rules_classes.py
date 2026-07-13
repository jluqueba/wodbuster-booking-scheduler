"""Unit tests for the class-list extractor (US-005 form uplift).

The pure :func:`extract_available_classes` takes a full LoadClass
payload and unions the two arrays it exposes:

- ``ClasesFiltradas`` (full daily schedule, fields ``NombreE`` +
  ``Hora``)
- ``Data`` (the operator's own weekly slots, fields ``Nombre`` +
  ``HoraComienzo``)

Tests exercise each source independently plus the union edge cases.

The wrapper :func:`fetch_available_classes` is covered indirectly by
the routes' component tests via a fake WodBuster client.
"""

from __future__ import annotations

from typing import Any

from wodbuster_worker.rules.classes import (
    AvailableClasses,
    extract_available_classes,
)


def _clases_filtradas(*items: dict[str, Any]) -> dict[str, Any]:
    return {"ClasesFiltradas": list(items)}


def _data(*items: dict[str, Any]) -> dict[str, Any]:
    return {"Data": list(items)}


# ---------------------------------------------------------------------------
# ClasesFiltradas source
# ---------------------------------------------------------------------------


def test_extract_from_clases_filtradas_dedupes_and_sorts() -> None:
    payload = _clases_filtradas(
        {"NombreE": "Cross Training", "Hora": "07:30:00"},
        {"NombreE": "Cross Training", "Hora": "18:30:00"},
        {"NombreE": "WOD", "Hora": "18:30:00"},
        {"NombreE": "Halterofilia", "Hora": "21:30:00"},
    )
    result = extract_available_classes(payload)
    assert isinstance(result, AvailableClasses)
    assert result.class_types == ["Cross Training", "Halterofilia", "WOD"]
    assert result.time_slots == ["07:30", "18:30", "21:30"]


def test_extract_strips_seconds_from_time() -> None:
    payload = _clases_filtradas({"NombreE": "WOD", "Hora": "07:30:00"})
    assert extract_available_classes(payload).time_slots == ["07:30"]


def test_extract_skips_items_without_required_fields() -> None:
    payload = _clases_filtradas(
        {"NombreE": "WOD", "Hora": "07:30:00"},
        {"NombreE": "WOD"},  # missing hora
        {"Hora": "18:30:00"},  # missing nombre
        {"NombreE": "", "Hora": ""},  # empty
        {"NombreE": "  ", "Hora": "20:30:00"},  # whitespace-only nombre
    )
    payload["ClasesFiltradas"].append("not a dict")  # type: ignore[arg-type]
    result = extract_available_classes(payload)
    assert result.class_types == ["WOD"]
    assert result.time_slots == ["07:30", "18:30", "20:30"]


def test_extract_empty_payload_returns_empty_lists() -> None:
    result = extract_available_classes({})
    assert result.class_types == []
    assert result.time_slots == []
    assert result.is_empty


def test_extract_ignores_malformed_time_strings() -> None:
    payload = _clases_filtradas(
        {"NombreE": "WOD", "Hora": "07:30:00"},  # good
        {"NombreE": "WOD", "Hora": "0730"},  # no colon
        {"NombreE": "WOD", "Hora": "7:30"},  # too short
    )
    assert extract_available_classes(payload).time_slots == ["07:30"]


# ---------------------------------------------------------------------------
# Data source
# ---------------------------------------------------------------------------


def test_extract_from_data_uses_nombre_and_horacomienzo() -> None:
    payload = _data(
        {"Id": 42, "Nombre": "WOD", "HoraComienzo": "21:30:00"},
        {"Id": 43, "Nombre": "Cross Training", "HoraComienzo": "07:30:00"},
    )
    result = extract_available_classes(payload)
    assert result.class_types == ["Cross Training", "WOD"]
    assert result.time_slots == ["07:30", "21:30"]


def test_extract_from_data_only_when_clases_filtradas_missing() -> None:
    """Regression: operator with populated Data but empty ClasesFiltradas
    must still get a non-empty picker."""
    payload = {
        "ClasesFiltradas": [],
        "Data": [
            {"Id": 42, "Nombre": "WOD", "HoraComienzo": "21:30:00"},
        ],
    }
    result = extract_available_classes(payload)
    assert result.class_types == ["WOD"]
    assert result.time_slots == ["21:30"]


# ---------------------------------------------------------------------------
# Union behaviour
# ---------------------------------------------------------------------------


def test_extract_unions_both_sources_and_dedupes() -> None:
    payload = {
        "ClasesFiltradas": [
            {"NombreE": "Cross Training", "Hora": "07:30:00"},
            {"NombreE": "WOD", "Hora": "21:30:00"},
        ],
        "Data": [
            # Same slot as one in ClasesFiltradas — deduped.
            {"Id": 42, "Nombre": "WOD", "HoraComienzo": "21:30:00"},
            # New slot only visible via Data.
            {"Id": 43, "Nombre": "Halterofilia", "HoraComienzo": "20:30:00"},
        ],
    }
    result = extract_available_classes(payload)
    assert result.class_types == ["Cross Training", "Halterofilia", "WOD"]
    assert result.time_slots == ["07:30", "20:30", "21:30"]


def test_extract_wrong_types_on_both_arrays_returns_empty() -> None:
    payload = {"ClasesFiltradas": "not a list", "Data": 42}
    result = extract_available_classes(payload)
    assert result.is_empty
