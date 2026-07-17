"""Unit tests for the class-list extractor (US-005 form uplift).

The pure :func:`extract_available_classes` takes a full LoadClass
payload and unions two sources:

- ``ClasesFiltradas`` — flat rows, fields ``NombreE`` + ``Hora``.
- ``Data`` — time-slot buckets, each carrying its own ``Hora`` plus
  ``Valores[j].Valor`` with the concrete class instance's
  ``Nombre`` + ``HoraComienzo``.

Tests exercise each source independently plus the union edge cases.

The wrapper :func:`fetch_available_classes` is covered indirectly by
the routes' component tests via a fake WodBuster client.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import pytest

from wodbuster_worker.rules import classes as classes_module
from wodbuster_worker.rules.classes import (
    AvailableClasses,
    extract_available_classes,
    fetch_available_classes,
)


def _clases_filtradas(*items: dict[str, Any]) -> dict[str, Any]:
    return {"ClasesFiltradas": list(items)}


def _data_bucket(hora: str, *instances: dict[str, Any]) -> dict[str, Any]:
    """Build a Data[i] time-slot bucket wrapping instances under Valor."""
    return {
        "Hora": hora,
        "Valores": [{"Valor": inst} for inst in instances],
    }


def _data(*buckets: dict[str, Any]) -> dict[str, Any]:
    return {"Data": list(buckets)}


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


def test_extract_from_data_walks_valores_valor_for_class_types() -> None:
    payload = _data(
        _data_bucket(
            "21:30:00",
            {"Id": 42, "Nombre": "WOD", "HoraComienzo": "21:30:00"},
        ),
        _data_bucket(
            "07:30:00",
            {"Id": 43, "Nombre": "Cross Training", "HoraComienzo": "07:30:00"},
        ),
    )
    result = extract_available_classes(payload)
    assert result.class_types == ["Cross Training", "WOD"]
    assert result.time_slots == ["07:30", "21:30"]


def test_extract_uses_bucket_hora_even_when_valores_missing_name() -> None:
    """Regression: buckets should still contribute their ``Hora`` to
    the time slots even when the nested ``Valor`` is unparseable."""
    payload = _data(
        _data_bucket("22:40:00"),  # empty Valores list
        {"Hora": "07:30:00"},  # bucket without Valores key at all
    )
    result = extract_available_classes(payload)
    assert result.class_types == []
    assert result.time_slots == ["07:30", "22:40"]


def test_extract_from_data_only_when_clases_filtradas_empty() -> None:
    """The real regression: prod payload had ClasesFiltradas=[] and
    Data populated with the operator's own slots. Must still yield
    a non-empty picker."""
    payload = {
        "ClasesFiltradas": [],
        "Data": [
            _data_bucket(
                "21:30:00",
                {"Id": 42, "Nombre": "WOD", "HoraComienzo": "21:30:00"},
            ),
        ],
    }
    result = extract_available_classes(payload)
    assert result.class_types == ["WOD"]
    assert result.time_slots == ["21:30"]


def test_extract_accepts_bare_dict_in_valores_without_wrapper() -> None:
    """Defensive: some entries may skip the ``Valor`` layer."""
    payload = {
        "Data": [
            {
                "Hora": "21:30:00",
                "Valores": [{"Id": 99, "Nombre": "WOD", "HoraComienzo": "21:30:00"}],
            }
        ]
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
            _data_bucket(
                "21:30:00",
                # Same slot as one in ClasesFiltradas — deduped.
                {"Id": 42, "Nombre": "WOD", "HoraComienzo": "21:30:00"},
            ),
            _data_bucket(
                "20:30:00",
                # New slot only visible via Data.
                {"Id": 43, "Nombre": "Halterofilia", "HoraComienzo": "20:30:00"},
            ),
        ],
    }
    result = extract_available_classes(payload)
    assert result.class_types == ["Cross Training", "Halterofilia", "WOD"]
    assert result.time_slots == ["07:30", "20:30", "21:30"]


def test_extract_wrong_types_on_both_arrays_returns_empty() -> None:
    payload = {"ClasesFiltradas": "not a list", "Data": 42}
    result = extract_available_classes(payload)
    assert result.is_empty


# ---------------------------------------------------------------------------
# fetch_available_classes week-scan behaviour
# ---------------------------------------------------------------------------


class _FakeStore:
    """Stand-in cookie store that ignores the session and returns a cookie."""

    def __init__(self, cookie: str | None) -> None:
        self._cookie = cookie

    def load(self, _session: Any, _operator_id: int) -> str | None:
        return self._cookie


class _FakeClient:
    """Returns a per-call payload and records the ``ticks`` it saw."""

    def __init__(self, payloads: list[dict[str, Any]]) -> None:
        self._payloads = payloads
        self.calls: list[int] = []

    def load_class(self, _cookie_value: str, ticks: int) -> SimpleNamespace:
        payload = self._payloads[len(self.calls)]
        self.calls.append(ticks)
        return SimpleNamespace(payload=payload)


@contextlib.contextmanager
def _null_session() -> Iterator[None]:
    yield None


def test_fetch_unions_day_specific_classes_across_the_week(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Saturday-only class surfaces even though it runs on one day.

    WodBuster scopes ``ClasesFiltradas`` to the queried day, so the
    picker probes all seven days and unions the results. Here only one
    day carries ``Endurance``; it must still appear in the combo.
    """
    per_day = [_clases_filtradas({"NombreE": "WOD", "Hora": "18:30:00"}) for _ in range(7)]
    per_day[3] = _clases_filtradas(
        {"NombreE": "WOD", "Hora": "18:30:00"},
        {"NombreE": "Endurance", "Hora": "10:00:00"},
    )
    client = _FakeClient(per_day)
    monkeypatch.setattr(classes_module, "get_session", _null_session)

    result = fetch_available_classes(_FakeStore("cookie"), client, operator_id=1)

    assert result is not None
    assert result.class_types == ["Endurance", "WOD"]
    assert result.time_slots == ["10:00", "18:30"]
    # One probe per day of the week, each a distinct day.
    assert len(client.calls) == 7
    assert len(set(client.calls)) == 7


def test_fetch_returns_none_when_no_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([])
    monkeypatch.setattr(classes_module, "get_session", _null_session)

    result = fetch_available_classes(_FakeStore(None), client, operator_id=1)

    assert result is None
    assert client.calls == []
