"""LoadClass payload parsers (US1.5, US1.7 support).

The booking executor needs to pick a concrete class instance out of a
LoadClass.ashx response and track ``SegundosHastaPublicacion`` to
align firing time. The client keeps the HTTP surface pure (raw dict);
parsing lives here so tests can drive it with synthetic payloads.

Vocabulary anchored on the Spanish field names WodBuster exposes:

- ``Data[]`` is a per-day list of *time-slot buckets*, not of
  individual class instances. Each bucket carries:

  - ``Data[i].Hora`` — ``HH:MM:SS`` start time for the bucket.
  - ``Data[i].Valores[]`` — list of concrete class instances at that
    time. Each entry wraps the payload under a ``Valor`` key.

- ``Data[i].Valores[j].Valor`` — the concrete class instance:

  - ``Id`` — integer instance id (the executor passes this to
    ``inscribir``). ``0`` when the row is a placeholder rather than a
    bookable instance.
  - ``Nombre`` — class-type label (``"WOD"``, ``"Cross Training"``).
  - ``HoraComienzo`` — ``HH:MM:SS`` start time (matches the parent
    bucket's ``Hora``).
  - ``TipoEstado`` — ``Inscribible`` / ``Borrable`` / ``Avisable``.
  - ``Plazas`` — total capacity (int).
  - ``AtletasEnListaDeEspera`` — waitlist length (int).

- ``SegundosHastaPublicacion`` — float. Positive means the reservation
  window is still in the future; negative means already open.

The nested shape was confirmed empirically by the
``/rules/api/classes/debug`` endpoint after an early implementation
tried to walk ``Data[]`` as if each entry were a class instance and
silently returned no slots. :func:`_iter_class_instances` isolates
that walk so both the executor and the picker read it the same way.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, Literal

# Enumerated string values observed in Phase 0. Kept as a Literal so
# call sites match on typed variants rather than string equality.
ClassStatus = Literal[
    "Inscribible",  # bookable — has free places
    "Borrable",  # already enrolled — cancel-able
    "Avisable",  # full — "notify me" available
    "Unknown",  # any other / missing status value
]


@dataclass(frozen=True)
class ClassSlot:
    """One bookable class instance parsed from a LoadClass payload."""

    id: int
    nombre: str
    hora_comienzo: str  # HH:MM (truncated from HH:MM:SS at parse time)
    tipo_estado: ClassStatus
    plazas: int | None
    waitlist_length: int | None


def parse_class_instance(row: Any) -> ClassSlot | None:
    """Convert a single class-instance dict into a :class:`ClassSlot`.

    Accepts either the wrapped form (``{"Valor": {...}}``) or a bare
    dict, so callers that already unwrapped ``Valor`` can pass the
    inner object. Returns ``None`` when the row is not a dict, when
    required fields are missing / of unexpected type, or when
    ``Id == 0`` (placeholder rows from the filtered calendar view).
    """
    if isinstance(row, dict) and isinstance(row.get("Valor"), dict):
        row = row["Valor"]
    if not isinstance(row, dict):
        return None
    raw_id = row.get("Id")
    if not isinstance(raw_id, int) or isinstance(raw_id, bool) or raw_id <= 0:
        return None
    nombre = row.get("Nombre")
    if not isinstance(nombre, str) or not nombre.strip():
        return None
    hora = row.get("HoraComienzo")
    if not isinstance(hora, str) or len(hora) < 5 or hora[2] != ":":
        return None

    return ClassSlot(
        id=raw_id,
        nombre=nombre.strip(),
        hora_comienzo=hora[:5],
        tipo_estado=_normalise_status(row.get("TipoEstado")),
        plazas=_optional_int(row.get("Plazas")),
        waitlist_length=_optional_int(row.get("AtletasEnListaDeEspera")),
    )


def extract_class_slots(payload: dict[str, Any]) -> list[ClassSlot]:
    """Return every parseable :class:`ClassSlot` from a LoadClass payload.

    Walks ``Data[i].Valores[j]`` and unwraps each ``Valor`` entry via
    :func:`parse_class_instance`. Malformed rows are dropped. Callers
    wanting a specific slot then filter by ``nombre`` + ``hora_comienzo``
    via :func:`find_matching_slot`.
    """
    return list(_iter_class_instances(payload))


def _iter_class_instances(payload: dict[str, Any]) -> Iterator[ClassSlot]:
    """Yield every parseable class instance under ``Data[].Valores[]``."""
    data = payload.get("Data")
    if not isinstance(data, list):
        return
    for bucket in data:
        if not isinstance(bucket, dict):
            continue
        valores = bucket.get("Valores")
        if not isinstance(valores, list):
            continue
        for entry in valores:
            parsed = parse_class_instance(entry)
            if parsed is not None:
                yield parsed


def find_matching_slot(
    slots: list[ClassSlot], *, class_type: str, class_time: str
) -> ClassSlot | None:
    """Return the first slot whose type + start time match, else ``None``.

    Matching is case-insensitive on ``class_type`` and exact on the
    ``HH:MM`` ``class_time``. The executor calls this once for the
    primary and again for the second shot; a missing return maps to
    the "class not visible" retry path (US1.7).
    """
    target_type = class_type.strip().lower()
    for slot in slots:
        if slot.nombre.lower() == target_type and slot.hora_comienzo == class_time:
            return slot
    return None


def extract_seconds_until_publication(payload: dict[str, Any]) -> float | None:
    """Return ``SegundosHastaPublicacion`` from the payload.

    ``None`` when the field is missing or not a number. Callers treat
    ``None`` as "server did not surface a countdown; fall back to the
    scheduler's own timer".
    """
    raw = payload.get("SegundosHastaPublicacion")
    if isinstance(raw, int | float) and not isinstance(raw, bool):
        return float(raw)
    return None


def _normalise_status(value: Any) -> ClassStatus:
    if isinstance(value, str):
        stripped = value.strip()
        if stripped in ("Inscribible", "Borrable", "Avisable"):
            # Literal narrowing: mypy needs the explicit typed return here.
            return stripped  # type: ignore[return-value]
    return "Unknown"


def _optional_int(value: Any) -> int | None:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return None


__all__ = [
    "ClassSlot",
    "ClassStatus",
    "extract_class_slots",
    "extract_seconds_until_publication",
    "find_matching_slot",
    "parse_class_instance",
]
