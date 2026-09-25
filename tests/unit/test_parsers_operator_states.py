"""Unit tests for the attendance-history parser (T-AST-003, ADR-0013).

Fixtures mirror the shape captured from a live gym on 2026-09-24: a day
returns 30 to 33 class instances, each carrying three athlete lists,
and each athlete entry carrying ``FechaEstado`` plus identity fields
that must never leave this layer.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime
from typing import Any

from wodbuster_worker.wodbuster_client.parsers import (
    OperatorClassState,
    operator_idu_to_guid,
    parse_fecha_estado,
    read_operator_states,
)

OPERATOR_IDU = "aae9b1c2d3e4f5061728394a5b6c7605"
OTHER_IDU = "11112222333344445555666677778888"


def _athlete(idu: str, *, fecha_estado: str | None = "20/09/2026 22:40:00") -> dict[str, Any]:
    """An athlete entry shaped like the real payload, identity included.

    The identity fields are present on purpose: the parser must see them
    and must not pass them on.
    """
    guid = operator_idu_to_guid(idu)
    entry: dict[str, Any] = {
        "Id": 94,
        "DisplayName": "Someone Else",
        "Url": f"/athlete/athletes.aspx?gid={guid}",
        "UrlFoto": f"https://cdn.wodbuster.com/static/atletas/a/a/e/{guid}.jpg",
        "ConfirmadaAsistencia": False,
        "EsNoPago": False,
        "TipoReserva": "Tarifa",
    }
    if fecha_estado is not None:
        entry["FechaEstado"] = fecha_estado
    return entry


def _valor(
    *,
    class_id: int = 47459,
    attending: list[dict[str, Any]] | None = None,
    removed: list[dict[str, Any]] | None = None,
    absent: list[dict[str, Any]] | None = None,
    plazas: int | None = 14,
    ever_full: bool = True,
    **overrides: Any,
) -> dict[str, Any]:
    valor: dict[str, Any] = {
        "Id": class_id,
        "Nombre": "Cross Training",
        "IdTipoEntrenamiento": 1,
        "HoraComienzo": "20:30:00",
        "AlgunMomentoLlena": ever_full,
        "AtletasEnListaDeEspera": 0,
        "AtletasEntrenando": attending if attending is not None else [],
        "AtletasBorradosVisibles": removed if removed is not None else [],
        "AtletasNoEntrenandoVisibles": absent if absent is not None else [],
        "Profesores": [],
    }
    if plazas is not None:
        valor["Plazas"] = plazas
    valor.update(overrides)
    return valor


def _payload(*valores: dict[str, Any]) -> dict[str, Any]:
    return {"Data": [{"Hora": "20:30:00", "Valores": [{"Valor": v} for v in valores]}]}


def test_operator_in_the_attending_list_reads_as_attended() -> None:
    crowd = [_athlete(OTHER_IDU) for _ in range(13)]
    payload = _payload(_valor(attending=[*crowd, _athlete(OPERATOR_IDU)]))

    states = read_operator_states(payload, operator_idu=OPERATOR_IDU)

    assert len(states) == 1
    state = states[0]
    assert state.state == "attended"
    assert state.class_id == 47459
    assert state.class_name == "Cross Training"
    assert state.class_type_id == 1
    assert state.hora_comienzo == "20:30"
    assert state.occupancy == 14
    assert state.capacity == 14
    assert state.ever_full is True
    assert state.reservation_type == "Tarifa"
    assert state.state_changed_at == datetime(2026, 9, 20, 22, 40, 0)


def test_operator_in_the_removed_list_reads_as_cancelled() -> None:
    """The observed case: removed at 18:29 from a class starting 20:30."""
    payload = _payload(
        _valor(
            class_id=47396,
            attending=[_athlete(OTHER_IDU) for _ in range(14)],
            removed=[_athlete(OPERATOR_IDU, fecha_estado="21/09/2026 18:29:00")],
        )
    )

    states = read_operator_states(payload, operator_idu=OPERATOR_IDU)

    assert len(states) == 1
    assert states[0].state == "cancelled"
    assert states[0].state_changed_at == datetime(2026, 9, 21, 18, 29, 0)
    # Occupancy still describes the class, not the operator's own list.
    assert states[0].occupancy == 14


def test_operator_in_the_absent_list_reads_as_no_show() -> None:
    payload = _payload(_valor(absent=[_athlete(OPERATOR_IDU)]))

    states = read_operator_states(payload, operator_idu=OPERATOR_IDU)

    assert [s.state for s in states] == ["no_show"]


def test_operator_in_no_list_yields_nothing() -> None:
    """A day the gym was open and the operator did not train."""
    payload = _payload(
        _valor(attending=[_athlete(OTHER_IDU) for _ in range(14)]),
        _valor(class_id=47460, attending=[_athlete(OTHER_IDU)]),
    )

    assert read_operator_states(payload, operator_idu=OPERATOR_IDU) == []


def test_attending_wins_when_the_operator_appears_twice() -> None:
    """Booked, removed, booked again: the attending list is how it ended."""
    payload = _payload(
        _valor(
            attending=[_athlete(OPERATOR_IDU, fecha_estado="22/09/2026 09:00:00")],
            removed=[_athlete(OPERATOR_IDU, fecha_estado="21/09/2026 18:29:00")],
        )
    )

    states = read_operator_states(payload, operator_idu=OPERATOR_IDU)

    assert [s.state for s in states] == ["attended"]
    assert states[0].state_changed_at == datetime(2026, 9, 22, 9, 0, 0)


def test_malformed_instances_are_skipped_without_raising() -> None:
    payload = _payload(
        {"Id": 0, "Nombre": "Cross Training", "HoraComienzo": "20:30:00"},
        {"Id": 1, "Nombre": "", "HoraComienzo": "20:30:00"},
        {"Id": 2, "Nombre": "Cross Training", "HoraComienzo": "bad"},
        _valor(class_id=3, attending=[_athlete(OPERATOR_IDU)]),
    )

    states = read_operator_states(payload, operator_idu=OPERATOR_IDU)

    assert [s.class_id for s in states] == [3]


def test_missing_capacity_yields_none_without_raising() -> None:
    payload = _payload(_valor(plazas=None, attending=[_athlete(OPERATOR_IDU)]))

    assert read_operator_states(payload, operator_idu=OPERATOR_IDU)[0].capacity is None


def test_missing_or_unparseable_state_instant_yields_none() -> None:
    missing = _payload(_valor(attending=[_athlete(OPERATOR_IDU, fecha_estado=None)]))
    garbage = _payload(_valor(attending=[_athlete(OPERATOR_IDU, fecha_estado="not a date")]))

    assert read_operator_states(missing, operator_idu=OPERATOR_IDU)[0].state_changed_at is None
    assert read_operator_states(garbage, operator_idu=OPERATOR_IDU)[0].state_changed_at is None


def test_empty_or_unexpected_payload_yields_nothing() -> None:
    assert read_operator_states({}, operator_idu=OPERATOR_IDU) == []
    assert read_operator_states({"Data": "nope"}, operator_idu=OPERATOR_IDU) == []
    assert read_operator_states({"Data": [{"Valores": None}]}, operator_idu=OPERATOR_IDU) == []


def test_result_carries_no_field_able_to_hold_a_third_party_identity() -> None:
    """INV-001 at the parser boundary.

    Asserted on the dataclass definition rather than on one instance, so
    adding an identity-bearing field later fails here even if no test
    fixture happens to populate it.
    """
    fields = {f.name for f in dataclasses.fields(OperatorClassState)}

    assert not fields & {"display_name", "url", "url_foto", "athletes", "entry", "raw"}
    assert fields == {
        "class_id",
        "class_name",
        "class_type_id",
        "hora_comienzo",
        "state",
        "state_changed_at",
        "reservation_type",
        "capacity",
        "occupancy",
        "ever_full",
    }


def test_no_third_party_value_survives_into_the_result() -> None:
    payload = _payload(
        _valor(
            attending=[_athlete(OTHER_IDU) for _ in range(13)] + [_athlete(OPERATOR_IDU)],
        )
    )

    rendered = repr(read_operator_states(payload, operator_idu=OPERATOR_IDU))

    assert "Someone Else" not in rendered
    assert "cdn.wodbuster.com" not in rendered
    assert "athletes.aspx" not in rendered


def test_parse_fecha_estado_rejects_non_strings() -> None:
    assert parse_fecha_estado(None) is None
    assert parse_fecha_estado(1234) is None
    assert parse_fecha_estado("2026-09-21 18:29:00") is None
    assert parse_fecha_estado(" 21/09/2026 18:29:00 ") == datetime(2026, 9, 21, 18, 29, 0)


def test_a_waitlist_entry_that_never_became_a_booking_yields_nothing() -> None:
    """CC-019, INV-005 boundary: an unconsumed waitlist entry must reach
    no metric, including the denominator of the abandonment rate.

    The exclusion is structural rather than a rule applied later. The
    parser reads the three athlete lists, and an athlete waiting for a
    place is in none of them; only the waitlist length is published, as
    a number with no identity attached.
    """
    crowd = [_athlete(OTHER_IDU) for _ in range(14)]
    payload = _payload(_valor(attending=crowd, plazas=14, ever_full=True, AtletasEnListaDeEspera=3))

    states = read_operator_states(payload, operator_idu=OPERATOR_IDU)

    assert states == []


def test_a_waitlist_that_turned_into_a_place_reads_as_attended() -> None:
    """The other half of the same rule: once the place is granted the
    athlete appears in the attending list like any other booking."""
    crowd = [_athlete(OTHER_IDU) for _ in range(13)]
    payload = _payload(_valor(attending=[*crowd, _athlete(OPERATOR_IDU)], AtletasEnListaDeEspera=2))

    states = read_operator_states(payload, operator_idu=OPERATOR_IDU)

    assert [state.state for state in states] == ["attended"]
