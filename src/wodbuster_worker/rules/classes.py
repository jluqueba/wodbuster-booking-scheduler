"""Distinct class-types and time-slots for the rules form (US-005).

The rule-creation form's dropdowns are seeded from the gym's live
schedule so operators pick from real values instead of typing free
text. Phase 0's ``LoadClass.ashx`` response exposes two arrays:

- ``ClasesFiltradas`` — the full daily schedule (all classes across
  the day, with fields ``NombreE`` for the class type name and
  ``Hora`` for the ``HH:MM:SS`` start time). Populated when the
  server returns the unfiltered view.
- ``Data`` — the operator's own slots for the queried week. Each
  entry carries ``Nombre`` and ``HoraComienzo``. Populated whenever
  the operator has enrolled bookings.

Historically we only read ``ClasesFiltradas``. Empirically it can
come back empty depending on the operator's session state and
recent booking activity, which left the picker with nothing to
offer. The current implementation unions both arrays so the picker
degrades gracefully: as long as the operator has any booking (or
the server returns the full schedule), the picker has entries.

Failure modes (no cookie on file, WodBuster unreachable, protocol
break) all collapse to ``None`` so the caller can render the form
in the disabled state. Every branch logs a one-line diagnostic
tagged ``rules.picker.*`` — invaluable when the operator reports
"the dropdown is empty" and the code path was silent before.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog

from ..persistence.cookie_store import CookieStore
from ..persistence.engine import get_session
from ..wodbuster_client.client import (
    WodBusterAuthError,
    WodBusterClient,
    WodBusterProtocolError,
    WodBusterTransportError,
)

_log = structlog.get_logger(__name__)


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

    Returns ``None`` when the cookie is missing or WodBuster refuses
    the call. Returns an :class:`AvailableClasses` (possibly empty)
    when the call succeeds but yields no parseable entries — the
    caller inspects ``is_empty`` to decide whether to disable the
    form.
    """
    with get_session() as session:
        cookie_value = store.load(session, operator_id)
    if cookie_value is None:
        _log.info("rules.picker.no_cookie", operator_id=operator_id)
        return None

    ticks = _today_ticks_utc()
    try:
        loaded = client.load_class(cookie_value, ticks)
    except WodBusterAuthError as exc:
        _log.warning(
            "rules.picker.auth_error", operator_id=operator_id, error=str(exc)
        )
        return None
    except (WodBusterTransportError, WodBusterProtocolError) as exc:
        _log.warning(
            "rules.picker.upstream_error",
            operator_id=operator_id,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None

    result = extract_available_classes(loaded.payload)
    _log.info(
        "rules.picker.fetched",
        operator_id=operator_id,
        class_types=len(result.class_types),
        time_slots=len(result.time_slots),
        clases_filtradas_len=_safe_len(loaded.payload.get("ClasesFiltradas")),
        data_len=_safe_len(loaded.payload.get("Data")),
    )
    return result


def extract_available_classes(payload: dict[str, Any]) -> AvailableClasses:
    """Extract class types + time slots from a LoadClass payload.

    Unions two sources:

    - ``ClasesFiltradas[i]``: ``NombreE`` (name), ``Hora`` (HH:MM:SS).
    - ``Data[i]``: ``Nombre`` (name), ``HoraComienzo`` (HH:MM:SS).

    Duplicates are collapsed via set membership; ``HH:MM:SS`` values
    are truncated to ``HH:MM`` because that is the format the rest
    of the system stores. Pure function so unit tests can drive it
    with synthetic payloads.
    """
    class_types: set[str] = set()
    time_slots: set[str] = set()

    for item in _iter_dicts(payload.get("ClasesFiltradas")):
        _accumulate(
            item,
            name_key="NombreE",
            time_key="Hora",
            class_types=class_types,
            time_slots=time_slots,
        )

    for item in _iter_dicts(payload.get("Data")):
        _accumulate(
            item,
            name_key="Nombre",
            time_key="HoraComienzo",
            class_types=class_types,
            time_slots=time_slots,
        )

    return AvailableClasses(
        class_types=sorted(class_types),
        time_slots=sorted(time_slots),
    )


def _iter_dicts(value: Any) -> list[dict[str, Any]]:
    """Yield dict entries from ``value`` when it is a list; else empty."""
    if not isinstance(value, list):
        return []
    return [entry for entry in value if isinstance(entry, dict)]


def _accumulate(
    item: dict[str, Any],
    *,
    name_key: str,
    time_key: str,
    class_types: set[str],
    time_slots: set[str],
) -> None:
    name = item.get(name_key)
    if isinstance(name, str) and name.strip():
        class_types.add(name.strip())
    time_value = item.get(time_key)
    if isinstance(time_value, str) and len(time_value) >= 5 and time_value[2] == ":":
        time_slots.add(time_value[:5])  # "HH:MM:SS" -> "HH:MM"


def _safe_len(value: Any) -> int | None:
    if isinstance(value, list):
        return len(value)
    return None


def _today_ticks_utc() -> int:
    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


__all__ = ["AvailableClasses", "extract_available_classes", "fetch_available_classes"]
