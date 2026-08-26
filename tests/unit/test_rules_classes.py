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
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from wodbuster_worker.booking.overrides import effective_slot_for
from wodbuster_worker.persistence.models import SchedulerRule
from wodbuster_worker.rules import classes as classes_module
from wodbuster_worker.rules.classes import (
    AvailableClasses,
    extract_available_classes,
    fetch_available_classes,
    fetch_classes_for_date,
)
from wodbuster_worker.wodbuster_client.client import (
    WodBusterAuthError,
    WodBusterProtocolError,
    WodBusterTransportError,
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
# ListClases source (pre-publication preview)
# ---------------------------------------------------------------------------


def test_extract_from_list_clases_when_day_not_yet_published() -> None:
    """Regression: a day WodBuster has not opened for booking yet still
    ships its schedule template under ``ListClases`` while
    ``ClasesFiltradas``/``Data`` stay empty. The picker must read it so
    a not-yet-published weekend class still surfaces in the combo."""
    payload = {
        "ClasesFiltradas": [],
        "Data": [],
        "SegundosHastaPublicacion": 17054.0,
        "ListClases": [
            {"Hora": "10:00:00", "NombreE": "Endurance", "IdE": 4, "Id": 46869},
            {"Hora": "08:00:00", "NombreE": "Open box sábado", "IdE": 8, "Id": 46868},
        ],
    }
    result = extract_available_classes(payload)
    assert result.class_types == ["Endurance", "Open box sábado"]
    assert result.time_slots == ["08:00", "10:00"]


def test_extract_unions_list_clases_with_clases_filtradas() -> None:
    payload = {
        "ClasesFiltradas": [{"NombreE": "WOD", "Hora": "18:30:00"}],
        "ListClases": [{"NombreE": "Endurance", "Hora": "10:00:00"}],
    }
    result = extract_available_classes(payload)
    assert result.class_types == ["Endurance", "WOD"]


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

    def load(self, _session: Any, _gym_account_id: int) -> str | None:
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

    result = fetch_available_classes(_FakeStore("cookie"), client, gym_account_id=1)

    assert result is not None
    assert result.class_types == ["Endurance", "WOD"]
    assert result.time_slots == ["10:00", "18:30"]
    # One probe per day of the week, each a distinct day.
    assert len(client.calls) == 7
    assert len(set(client.calls)) == 7


def test_fetch_returns_none_when_no_cookie(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _FakeClient([])
    monkeypatch.setattr(classes_module, "get_session", _null_session)

    result = fetch_available_classes(_FakeStore(None), client, gym_account_id=1)

    assert result is None
    assert client.calls == []


# ---------------------------------------------------------------------------
# fetch_classes_for_date (T-BDO-004)
# ---------------------------------------------------------------------------


def _instance(slot_id: int, nombre: str, hora: str) -> dict[str, Any]:
    return {
        "Id": slot_id,
        "Nombre": nombre,
        "HoraComienzo": f"{hora}:00",
        "TipoEstado": "Inscribible",
        "Plazas": 12,
    }


def _day_payload(*instances: dict[str, Any]) -> dict[str, Any]:
    buckets: dict[str, list[dict[str, Any]]] = {}
    for inst in instances:
        buckets.setdefault(inst["HoraComienzo"], []).append({"Valor": inst})
    return {"Data": [{"Hora": hora, "Valores": v} for hora, v in buckets.items()]}


def _probe_rule() -> SchedulerRule:
    rule = SchedulerRule(
        gym_account_id=1,
        day_of_week=2,
        class_type="WOD",
        class_time="07:00",
        booking_opens_days_before=2,
        booking_opens_at="21:30",
        active=True,
    )
    rule.id = 7
    return rule


@pytest.fixture
def _madrid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")


def _probe(
    monkeypatch: pytest.MonkeyPatch,
    client: _FakeClient,
    *,
    cookie: str | None = "cookie",
    target_date: date = date(2026, 7, 15),
    class_time: str = "07:00",
) -> tuple[Any, _FakeClient]:
    monkeypatch.setattr(classes_module, "get_session", _null_session)
    slot = effective_slot_for(_probe_rule(), target_date, class_time)
    result = fetch_classes_for_date(
        _FakeStore(cookie),
        client,  # type: ignore[arg-type]
        1,
        target_slot=slot,
    )
    return result, client


def test_fetch_for_date_returns_published_schedule_with_real_pairs(
    _madrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeClient(
        [
            _day_payload(
                _instance(1, "WOD", "07:00"),
                _instance(2, "Endurance", "18:30"),
            )
        ]
    )

    schedule, client = _probe(monkeypatch, client)

    assert schedule is not None
    assert schedule.published is True
    assert schedule.target_date == date(2026, 7, 15)
    assert schedule.class_types == ["Endurance", "WOD"]
    assert schedule.time_slots == ["07:00", "18:30"]
    # One probe for the date, not seven for the week.
    assert len(client.calls) == 1


def test_fetch_for_date_has_matches_only_a_pair_present_on_that_date(
    _madrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeClient(
        [
            _day_payload(
                _instance(1, "WOD", "07:00"),
                _instance(2, "Endurance", "18:30"),
            )
        ]
    )

    schedule, _ = _probe(monkeypatch, client)

    assert schedule is not None
    assert schedule.has("WOD", "07:00") is True
    # Endurance exists on this date, but not at 07:00.
    assert schedule.has("Endurance", "07:00") is False
    assert schedule.has("Yoga", "18:30") is False


def test_fetch_for_date_reports_unpublished_when_the_day_carries_no_class(
    _madrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    schedule, _ = _probe(monkeypatch, _FakeClient([{"Data": []}]))

    assert schedule is not None
    assert schedule.published is False
    assert schedule.slots == []


def test_fetch_for_date_returns_none_without_a_cookie(
    _madrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _FakeClient([])

    schedule, client = _probe(monkeypatch, client, cookie=None)

    assert schedule is None
    assert client.calls == []


@pytest.mark.parametrize(
    "error",
    [
        WodBusterAuthError("rejected"),
        WodBusterTransportError("boom"),
        WodBusterProtocolError("not json"),
    ],
)
def test_fetch_for_date_returns_none_when_the_probe_fails(
    _madrid: None, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    class _FailingClient(_FakeClient):
        def load_class(self, _cookie_value: str, ticks: int) -> SimpleNamespace:
            self.calls.append(ticks)
            raise error

    schedule, _ = _probe(monkeypatch, _FailingClient([]))

    assert schedule is None


def test_fetch_for_date_uses_utc_midnight_ticks_of_the_effective_slot(
    _madrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe must read the day the executor will read.

    A 00:30 Madrid class on 15 July is 22:30 UTC on the 14th, so the
    ticks are UTC midnight of the 14th while the schedule is still
    labelled with the operator-local 15th. Using the picker's
    local-midnight convention here would probe the wrong day.
    """
    client = _FakeClient([_day_payload(_instance(1, "WOD", "00:30"))])

    schedule, client = _probe(
        monkeypatch, client, target_date=date(2026, 7, 15), class_time="00:30"
    )

    assert schedule is not None
    assert schedule.target_date == date(2026, 7, 15)
    assert client.calls == [int(datetime(2026, 7, 14, tzinfo=UTC).timestamp())]


def test_fetch_for_date_ticks_survive_the_spring_forward_transition(
    _madrid: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """29 March 2026 is the Madrid spring-forward day (CET -> CEST)."""
    client = _FakeClient([_day_payload(_instance(1, "WOD", "07:00"))])

    schedule, client = _probe(
        monkeypatch, client, target_date=date(2026, 3, 29), class_time="07:00"
    )

    assert schedule is not None
    assert schedule.target_date == date(2026, 3, 29)
    # 07:00 CEST (UTC+2) = 05:00 UTC on the same calendar day.
    assert client.calls == [int(datetime(2026, 3, 29, tzinfo=UTC).timestamp())]
