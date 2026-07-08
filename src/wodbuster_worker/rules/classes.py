"""Distinct class-types and time-slots for the rules form (US-005).

The rule-creation form's dropdowns are seeded from the gym's live
schedule so operators pick from real values instead of typing free
text. Phase 0's ``LoadClass.ashx`` response returns the full daily
schedule under ``ClasesFiltradas``; this module reads that array,
extracts the distinct ``NombreE`` (class type) and ``Hora`` (HH:MM:SS)
values, and returns them de-duped and sorted.

A single day's schedule covers the full daily rotation. Weekly
variation (some classes only on some days) is out of scope for the
first pass — worst case the operator can still edit an existing rule
to a class-type that wasn't in the picker.

Failure modes (no cookie on file, WodBuster unreachable, protocol
break) all collapse to ``None`` so the caller can fall back to
free-text inputs without a hard error.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..persistence.cookie_store import CookieStore
from ..persistence.engine import get_session
from ..wodbuster_client.client import (
    WodBusterAuthError,
    WodBusterClient,
    WodBusterProtocolError,
    WodBusterTransportError,
)


@dataclass(frozen=True)
class AvailableClasses:
    """Picker source for the rules form.

    ``class_types`` and ``time_slots`` are both sorted (alphabetical
    and chronological respectively) so the templates can iterate
    without re-sorting.
    """

    class_types: list[str]
    time_slots: list[str]

    @property
    def is_empty(self) -> bool:
        return not self.class_types and not self.time_slots


def fetch_available_classes(
    store: CookieStore,
    client: WodBusterClient,
    operator_id: int,
) -> AvailableClasses | None:
    """Probe WodBuster once and return the distinct class/time picker set.

    Returns ``None`` on any recoverable failure so the caller can
    render the form with free-text inputs instead. Callers should log
    the failure at their own layer — this function stays quiet so it
    can be swapped for a cached variant later without changing the
    log signature.
    """
    with get_session() as session:
        cookie_value = store.load(session, operator_id)
    if cookie_value is None:
        return None

    ticks = _today_ticks_utc()
    try:
        loaded = client.load_class(cookie_value, ticks)
    except (
        WodBusterAuthError,
        WodBusterTransportError,
        WodBusterProtocolError,
    ):
        return None

    clases = loaded.payload.get("ClasesFiltradas")
    if not isinstance(clases, list):
        return None

    return extract_available_classes(clases)


def extract_available_classes(items: list[object]) -> AvailableClasses:
    """De-dupe and sort the ``ClasesFiltradas`` array. Pure function.

    Split from :func:`fetch_available_classes` so unit tests can drive
    the extraction with synthetic payloads (no cookie, no HTTP).
    """
    class_types: set[str] = set()
    time_slots: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        nombre = item.get("NombreE")
        if isinstance(nombre, str) and nombre.strip():
            class_types.add(nombre.strip())
        hora = item.get("Hora")
        if isinstance(hora, str) and len(hora) >= 5 and hora[2] == ":":
            time_slots.add(hora[:5])  # "HH:MM:SS" -> "HH:MM"
    return AvailableClasses(
        class_types=sorted(class_types),
        time_slots=sorted(time_slots),
    )


def _today_ticks_utc() -> int:
    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


__all__ = ["AvailableClasses", "extract_available_classes", "fetch_available_classes"]
