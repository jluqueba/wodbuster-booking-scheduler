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


def find_slot_by_time(slots: list[ClassSlot], *, class_time: str) -> ClassSlot | None:
    """Return the first slot starting at ``class_time`` (HH:MM), else ``None``.

    Time-only lookup used by the manual booking path (US8.1): the
    operator picks a date + time from the web ``/book-now`` form or
    Telegram ``/bookclass`` and gives no class name, so the service
    resolves the class type from whichever class runs at that time.
    Matching is exact on the ``HH:MM`` start time.
    """
    for slot in slots:
        if slot.hora_comienzo == class_time:
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


# ---------------------------------------------------------------------------
# Enrollment detection (booking success signal)
# ---------------------------------------------------------------------------
#
# The booking-result ``Res`` field and the per-slot ``TipoEstado`` marker
# both turned out to be unreliable against the live WodBuster API (the
# vocabularies in this module and in the client's ``Res`` classifier were
# educated guesses from the Spanish UI that Phase 0 never confirmed with a
# real response body). Empirically, a successful booking is expressed by
# the operator appearing in the target slot's ``AtletasEntrenando`` list —
# each athlete entry carries a ``Url`` of the form
# ``/athlete/athletes.aspx?gid=<idu-as-guid>``. This block turns that
# observation into the authoritative success signal for the executor.


@dataclass(frozen=True)
class SlotEnrollment:
    """Enrollment status of the operator in a single class instance."""

    found: bool  # the slot id was present in the payload
    enrolled: bool  # the operator is in AtletasEntrenando
    occupied: int  # number of enrolled athletes
    capacity: int | None  # Plazas, when present

    @property
    def is_full(self) -> bool:
        """True when the slot has no free places left."""
        return self.capacity is not None and self.occupied >= self.capacity


def operator_idu_to_guid(idu: str) -> str:
    """Format a 32-hex WodBuster ``idu`` as a dashed lowercase GUID.

    The ``idu`` travels through URLs without dashes, but the athlete
    ``Url`` in ``AtletasEntrenando`` carries the dashed GUID form. When
    the input is not a 32-hex string it is returned lower-cased and
    stripped so callers can still match on it verbatim.
    """
    normalised = idu.strip().replace("-", "").lower()
    if len(normalised) != 32:
        return idu.strip().lower()
    return (
        f"{normalised[0:8]}-{normalised[8:12]}-{normalised[12:16]}-"
        f"{normalised[16:20]}-{normalised[20:]}"
    )


def read_target_enrollment(
    payload: dict[str, Any], *, slot_id: int, operator_idu: str
) -> SlotEnrollment:
    """Return whether the operator is enrolled in class instance ``slot_id``.

    Walks the raw ``Data[].Valores[].Valor`` objects, matches the one
    whose ``Id`` equals ``slot_id``, and checks that slot's
    ``AtletasEntrenando`` for the operator's identifier. Returns a
    ``SlotEnrollment`` with ``found=False`` when the slot is absent (for
    example a filtered calendar view that dropped it).
    """
    guid = operator_idu_to_guid(operator_idu)
    raw = operator_idu.strip().lower()
    for valor in _iter_raw_valores(payload):
        if valor.get("Id") != slot_id or isinstance(valor.get("Id"), bool):
            continue
        atletas = valor.get("AtletasEntrenando")
        athletes = atletas if isinstance(atletas, list) else []
        enrolled = any(_athlete_is_operator(a, guid, raw) for a in athletes)
        return SlotEnrollment(
            found=True,
            enrolled=enrolled,
            occupied=len(athletes),
            capacity=_optional_int(valor.get("Plazas")),
        )
    return SlotEnrollment(found=False, enrolled=False, occupied=0, capacity=None)


def _athlete_is_operator(athlete: Any, guid: str, raw: str) -> bool:
    """True when an ``AtletasEntrenando`` entry belongs to the operator.

    Primary signal is the athlete ``Url`` (``?gid=<idu-guid>``); as a
    safety net any string field carrying the identifier also matches, so
    a future field rename does not silently break detection.
    """
    if not isinstance(athlete, dict):
        return False
    for value in athlete.values():
        if isinstance(value, str):
            lowered = value.lower()
            if guid in lowered or raw in lowered:
                return True
    return False


def _iter_raw_valores(payload: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Yield each raw ``Valor`` object under ``Data[].Valores[]``."""
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
            if isinstance(entry, dict) and isinstance(entry.get("Valor"), dict):
                yield entry["Valor"]


__all__ = [
    "ClassSlot",
    "ClassStatus",
    "SlotEnrollment",
    "extract_class_slots",
    "extract_seconds_until_publication",
    "find_matching_slot",
    "find_slot_by_time",
    "operator_idu_to_guid",
    "parse_class_instance",
    "read_target_enrollment",
]
