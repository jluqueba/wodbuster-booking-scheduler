"""LoadClass payload parsers (US1.5, US1.7 support).

The booking executor needs to pick a concrete class instance out of a
LoadClass.ashx response and track ``SegundosHastaPublicacion`` to
align firing time. The client keeps the HTTP surface pure (raw dict);
parsing lives here so tests can drive it with synthetic payloads.

Vocabulary anchored on the Spanish field names WodBuster exposes:

- ``Data[]`` — filtered list of the operator's slots for the queried
  week. Each row is a class instance.
- ``Data[i].Id`` — integer instance id (the executor passes this to
  ``inscribir``). ``0`` when the row is a placeholder rather than a
  bookable instance.
- ``Data[i].Nombre`` — class-type label (e.g. ``"WOD"``,
  ``"Cross Training"``).
- ``Data[i].HoraComienzo`` — ``HH:MM:SS`` start time.
- ``Data[i].TipoEstado`` — ``Inscribible`` / ``Borrable`` / ``Avisable``.
- ``Data[i].Plazas`` — total capacity (int).
- ``Data[i].AtletasEnListaDeEspera`` — waitlist length (int).
- ``SegundosHastaPublicacion`` — float. Positive means the reservation
  window is still in the future; negative means already open.
"""

from __future__ import annotations

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
    """One bookable class instance parsed from ``Data[]``."""

    id: int
    nombre: str
    hora_comienzo: str  # HH:MM (truncated from HH:MM:SS at parse time)
    tipo_estado: ClassStatus
    plazas: int | None
    waitlist_length: int | None


def parse_data_row(row: Any) -> ClassSlot | None:
    """Convert one raw ``Data[i]`` dict into a :class:`ClassSlot`.

    Returns ``None`` when the row is not a dict, or when the required
    fields (``Id``, ``Nombre``, ``HoraComienzo``) are missing or of an
    unexpected type. Placeholder rows with ``Id == 0`` are also
    filtered out — Phase 0 confirmed those come from the filtered
    calendar view and are not bookable.
    """
    if not isinstance(row, dict):
        return None
    raw_id = row.get("Id")
    if not isinstance(raw_id, int) or raw_id <= 0:
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

    Reads the ``Data`` key and drops any row that :func:`parse_data_row`
    rejects. Callers wanting a specific slot then filter by
    ``nombre`` / ``hora_comienzo`` — see :func:`find_matching_slot`.
    """
    raw = payload.get("Data")
    if not isinstance(raw, list):
        return []
    slots: list[ClassSlot] = []
    for row in raw:
        parsed = parse_data_row(row)
        if parsed is not None:
            slots.append(parsed)
    return slots


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
    "parse_data_row",
]
