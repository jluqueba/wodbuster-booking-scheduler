"""Unit tests for LoadClass payload parsers (US1.5, US1.7 support).

Drives :mod:`wodbuster_worker.wodbuster_client.parsers` with
synthetic payloads shaped like the real Phase-0 LoadClass response:
``Data[]`` is a list of time-slot buckets, and each bucket carries a
``Valores[j].Valor`` object with the concrete class instance
(``Id``, ``Nombre``, ``HoraComienzo``, ...).

Covers the parsing edges the executor cares about: unwrapping the
``Valor`` layer, skipping placeholder rows (``Id == 0``), rejecting
malformed rows, matching a specific slot by class type and start
time, and reading the ``SegundosHastaPublicacion`` countdown.
"""

from __future__ import annotations

from typing import Any

import pytest

from wodbuster_worker.wodbuster_client.parsers import (
    ClassSlot,
    extract_class_slots,
    extract_seconds_until_publication,
    find_matching_slot,
    parse_class_instance,
)


def _valor(**overrides: Any) -> dict[str, Any]:
    """Build a valid class-instance object with sensible defaults."""
    base: dict[str, Any] = {
        "Id": 45654,
        "Nombre": "WOD",
        "HoraComienzo": "21:30:00",
        "TipoEstado": "Inscribible",
        "Plazas": 16,
        "AtletasEnListaDeEspera": 0,
    }
    base.update(overrides)
    return base


def _bucket(hora: str = "21:30:00", *instances: dict[str, Any]) -> dict[str, Any]:
    """Wrap ``instances`` under ``Valores[j].Valor`` inside a Data bucket."""
    return {
        "Hora": hora,
        "Valores": [{"Valor": inst} for inst in instances],
    }


# ---------------------------------------------------------------------------
# parse_class_instance
# ---------------------------------------------------------------------------


def test_parse_class_instance_unwraps_valor() -> None:
    """Wrapped form ``{"Valor": {...}}`` and bare dict both accepted."""
    inst = _valor()
    assert parse_class_instance({"Valor": inst}) == parse_class_instance(inst)


def test_parse_class_instance_full_row_returns_slot() -> None:
    slot = parse_class_instance(_valor())

    assert slot == ClassSlot(
        id=45654,
        nombre="WOD",
        hora_comienzo="21:30",
        tipo_estado="Inscribible",
        plazas=16,
        waitlist_length=0,
    )


def test_parse_class_instance_truncates_hora_comienzo_to_hh_mm() -> None:
    slot = parse_class_instance(_valor(HoraComienzo="07:00:00"))
    assert slot is not None
    assert slot.hora_comienzo == "07:00"


def test_parse_class_instance_placeholder_id_is_skipped() -> None:
    """``Id == 0`` marks filtered-view placeholders; not bookable."""
    assert parse_class_instance(_valor(Id=0)) is None


@pytest.mark.parametrize("bad_id", [None, "not-an-int", -1, 0.5])
def test_parse_class_instance_rejects_non_positive_int_id(bad_id: Any) -> None:
    assert parse_class_instance(_valor(Id=bad_id)) is None


@pytest.mark.parametrize("bad_nombre", [None, "", "   ", 123])
def test_parse_class_instance_rejects_empty_or_missing_nombre(
    bad_nombre: Any,
) -> None:
    assert parse_class_instance(_valor(Nombre=bad_nombre)) is None


@pytest.mark.parametrize("bad_hora", [None, "", "21", "21:3", "2130", "not-a-time"])
def test_parse_class_instance_rejects_malformed_hora(bad_hora: Any) -> None:
    assert parse_class_instance(_valor(HoraComienzo=bad_hora)) is None


def test_parse_class_instance_non_dict_returns_none() -> None:
    assert parse_class_instance("not-a-dict") is None
    assert parse_class_instance(None) is None
    assert parse_class_instance([]) is None


def test_parse_class_instance_unknown_tipo_estado_is_normalised() -> None:
    slot = parse_class_instance(_valor(TipoEstado="SomeNewState"))
    assert slot is not None
    assert slot.tipo_estado == "Unknown"


def test_parse_class_instance_optional_int_fields_missing_return_none() -> None:
    slot = parse_class_instance(_valor(Plazas=None, AtletasEnListaDeEspera="x"))
    assert slot is not None
    assert slot.plazas is None
    assert slot.waitlist_length is None


def test_parse_class_instance_bool_is_not_treated_as_int() -> None:
    """``isinstance(True, int)`` is True in Python; guard against
    booleans sneaking in as capacity or id values."""
    assert parse_class_instance(_valor(Id=True)) is None
    slot = parse_class_instance(_valor(Plazas=True))
    assert slot is not None
    assert slot.plazas is None


# ---------------------------------------------------------------------------
# extract_class_slots
# ---------------------------------------------------------------------------


def test_extract_class_slots_walks_data_buckets_valores_valor() -> None:
    payload = {
        "Data": [
            _bucket("07:30:00", _valor(Id=1, Nombre="Cross Training", HoraComienzo="07:30:00")),
            _bucket("21:30:00", _valor(Id=2, Nombre="WOD", HoraComienzo="21:30:00")),
        ]
    }

    slots = extract_class_slots(payload)

    assert [s.id for s in slots] == [1, 2]
    assert [s.nombre for s in slots] == ["Cross Training", "WOD"]


def test_extract_class_slots_skips_placeholder_and_malformed() -> None:
    payload = {
        "Data": [
            _bucket(
                "21:30:00",
                _valor(Id=1),
                _valor(Id=0, Nombre="placeholder"),  # skipped
            ),
            {"Hora": "07:30:00", "Valores": "not-a-list"},  # bucket ignored
            "not-a-dict",  # bucket ignored
            _bucket(
                "20:30:00",
                _valor(Id=99, Nombre="Halterofilia", HoraComienzo="20:30:00"),
            ),
        ]
    }

    slots = extract_class_slots(payload)
    assert [s.id for s in slots] == [1, 99]


def test_extract_class_slots_missing_data_key_returns_empty() -> None:
    assert extract_class_slots({}) == []


def test_extract_class_slots_wrong_type_returns_empty() -> None:
    assert extract_class_slots({"Data": "not-a-list"}) == []


def test_extract_class_slots_accepts_bare_dict_in_valores() -> None:
    """Some payloads may omit the ``Valor`` wrapper — still parseable."""
    payload = {
        "Data": [
            {
                "Hora": "21:30:00",
                "Valores": [_valor(Id=42, Nombre="WOD", HoraComienzo="21:30:00")],
            },
        ]
    }
    slots = extract_class_slots(payload)
    assert len(slots) == 1
    assert slots[0].id == 42


# ---------------------------------------------------------------------------
# find_matching_slot
# ---------------------------------------------------------------------------


def _slot(**overrides: Any) -> ClassSlot:
    base: dict[str, Any] = {
        "id": 1,
        "nombre": "WOD",
        "hora_comienzo": "21:30",
        "tipo_estado": "Inscribible",
        "plazas": 16,
        "waitlist_length": 0,
    }
    base.update(overrides)
    return ClassSlot(**base)


def test_find_matching_slot_exact_match_returns_slot() -> None:
    slots = [
        _slot(id=1, nombre="Cross Training", hora_comienzo="07:30"),
        _slot(id=2, nombre="WOD", hora_comienzo="21:30"),
    ]
    match = find_matching_slot(slots, class_type="WOD", class_time="21:30")
    assert match is not None
    assert match.id == 2


def test_find_matching_slot_class_type_is_case_insensitive() -> None:
    slots = [_slot(nombre="Cross Training", hora_comienzo="07:30")]
    match = find_matching_slot(slots, class_type="cross training", class_time="07:30")
    assert match is not None


def test_find_matching_slot_time_must_match_exactly() -> None:
    slots = [_slot(nombre="WOD", hora_comienzo="21:30")]
    assert find_matching_slot(slots, class_type="WOD", class_time="21:00") is None


def test_find_matching_slot_no_match_returns_none() -> None:
    slots = [_slot(nombre="WOD", hora_comienzo="21:30")]
    assert find_matching_slot(slots, class_type="Halterofilia", class_time="21:30") is None


def test_find_matching_slot_returns_first_match_when_duplicates_exist() -> None:
    slots = [
        _slot(id=10, nombre="WOD", hora_comienzo="21:30"),
        _slot(id=11, nombre="WOD", hora_comienzo="21:30"),
    ]
    match = find_matching_slot(slots, class_type="WOD", class_time="21:30")
    assert match is not None
    assert match.id == 10


# ---------------------------------------------------------------------------
# extract_seconds_until_publication
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (-233241.55, -233241.55),
        (0, 0.0),
        (1.5, 1.5),
        (5, 5.0),
    ],
)
def test_extract_seconds_until_publication_returns_float(value: Any, expected: float) -> None:
    assert extract_seconds_until_publication({"SegundosHastaPublicacion": value}) == expected


@pytest.mark.parametrize(
    "value",
    [None, "not-a-number", True, False, [1.0]],
)
def test_extract_seconds_until_publication_bad_types_return_none(value: Any) -> None:
    assert extract_seconds_until_publication({"SegundosHastaPublicacion": value}) is None


def test_extract_seconds_until_publication_missing_key_returns_none() -> None:
    assert extract_seconds_until_publication({}) is None
