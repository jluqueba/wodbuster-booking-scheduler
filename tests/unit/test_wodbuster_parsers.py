"""Unit tests for LoadClass payload parsers (US1.5, US1.7 support).

Drives :mod:`wodbuster_worker.wodbuster_client.parsers` with synthetic
payloads shaped like the Phase-0 LoadClass.ashx response. Covers the
parsing edges the executor will care about: skipping placeholder rows
(``Id == 0``), rejecting malformed rows, matching a specific slot by
class type and start time, and reading the ``SegundosHastaPublicacion``
countdown.
"""

from __future__ import annotations

from typing import Any

import pytest

from wodbuster_worker.wodbuster_client.parsers import (
    ClassSlot,
    extract_class_slots,
    extract_seconds_until_publication,
    find_matching_slot,
    parse_data_row,
)


def _row(**overrides: Any) -> dict[str, Any]:
    """Build a valid ``Data[i]`` row with sensible defaults."""
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


# ---------------------------------------------------------------------------
# parse_data_row
# ---------------------------------------------------------------------------


def test_parse_data_row_full_row_returns_slot() -> None:
    slot = parse_data_row(_row())

    assert slot == ClassSlot(
        id=45654,
        nombre="WOD",
        hora_comienzo="21:30",
        tipo_estado="Inscribible",
        plazas=16,
        waitlist_length=0,
    )


def test_parse_data_row_truncates_hora_comienzo_to_hh_mm() -> None:
    slot = parse_data_row(_row(HoraComienzo="07:00:00"))

    assert slot is not None
    assert slot.hora_comienzo == "07:00"


def test_parse_data_row_placeholder_id_is_skipped() -> None:
    """Rows with ``Id == 0`` come from the filtered-view fallback and
    are not bookable — the parser must drop them."""
    assert parse_data_row(_row(Id=0)) is None


@pytest.mark.parametrize("bad_id", [None, "not-an-int", -1, 0.5])
def test_parse_data_row_rejects_non_positive_int_id(bad_id: Any) -> None:
    assert parse_data_row(_row(Id=bad_id)) is None


@pytest.mark.parametrize("bad_nombre", [None, "", "   ", 123])
def test_parse_data_row_rejects_empty_or_missing_nombre(bad_nombre: Any) -> None:
    assert parse_data_row(_row(Nombre=bad_nombre)) is None


@pytest.mark.parametrize("bad_hora", [None, "", "21", "21:3", "2130", "not-a-time"])
def test_parse_data_row_rejects_malformed_hora(bad_hora: Any) -> None:
    assert parse_data_row(_row(HoraComienzo=bad_hora)) is None


def test_parse_data_row_non_dict_returns_none() -> None:
    assert parse_data_row("not-a-dict") is None
    assert parse_data_row(None) is None
    assert parse_data_row([]) is None


def test_parse_data_row_unknown_tipo_estado_is_normalised() -> None:
    slot = parse_data_row(_row(TipoEstado="SomeNewState"))
    assert slot is not None
    assert slot.tipo_estado == "Unknown"


def test_parse_data_row_optional_int_fields_missing_return_none() -> None:
    slot = parse_data_row(_row(Plazas=None, AtletasEnListaDeEspera="x"))
    assert slot is not None
    assert slot.plazas is None
    assert slot.waitlist_length is None


def test_parse_data_row_bool_is_not_treated_as_int() -> None:
    # ``isinstance(True, int)`` is True in Python; parsers must guard
    # against booleans sneaking in as capacity values.
    slot = parse_data_row(_row(Plazas=True))
    assert slot is not None
    assert slot.plazas is None


# ---------------------------------------------------------------------------
# extract_class_slots
# ---------------------------------------------------------------------------


def test_extract_class_slots_skips_invalid_rows() -> None:
    payload = {
        "Data": [
            _row(),
            _row(Id=0, Nombre="Placeholder"),  # skipped: Id=0
            "not-a-dict",  # skipped
            _row(Id=99999, Nombre="Cross Training", HoraComienzo="07:30:00"),
        ]
    }

    slots = extract_class_slots(payload)

    assert [s.id for s in slots] == [45654, 99999]
    assert [s.nombre for s in slots] == ["WOD", "Cross Training"]


def test_extract_class_slots_missing_data_key_returns_empty() -> None:
    assert extract_class_slots({}) == []


def test_extract_class_slots_wrong_type_returns_empty() -> None:
    assert extract_class_slots({"Data": "not-a-list"}) == []


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
    match = find_matching_slot(
        slots, class_type="cross training", class_time="07:30"
    )
    assert match is not None


def test_find_matching_slot_time_must_match_exactly() -> None:
    slots = [_slot(nombre="WOD", hora_comienzo="21:30")]
    assert find_matching_slot(slots, class_type="WOD", class_time="21:00") is None


def test_find_matching_slot_no_match_returns_none() -> None:
    slots = [_slot(nombre="WOD", hora_comienzo="21:30")]
    assert find_matching_slot(slots, class_type="Halterofilia", class_time="21:30") is None


def test_find_matching_slot_returns_first_match_when_duplicates_exist() -> None:
    # Duplicate ids would be a server bug but the parser must not
    # crash — pick the first row so ordering matches server output.
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
def test_extract_seconds_until_publication_returns_float(
    value: Any, expected: float
) -> None:
    assert extract_seconds_until_publication({"SegundosHastaPublicacion": value}) == expected


@pytest.mark.parametrize(
    "value",
    [None, "not-a-number", True, False, [1.0]],
)
def test_extract_seconds_until_publication_bad_types_return_none(value: Any) -> None:
    assert extract_seconds_until_publication({"SegundosHastaPublicacion": value}) is None


def test_extract_seconds_until_publication_missing_key_returns_none() -> None:
    assert extract_seconds_until_publication({}) is None
