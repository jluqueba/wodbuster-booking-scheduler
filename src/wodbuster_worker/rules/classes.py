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

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog

from ..booking.overrides import local_date_for_slot
from ..persistence.cookie_store import CookieStore
from ..persistence.engine import get_session
from ..scheduler.clock import midnight_utc_ticks
from ..wodbuster_client.client import (
    WodBusterAuthError,
    WodBusterClient,
    WodBusterProtocolError,
    WodBusterTransportError,
)
from ..wodbuster_client.parsers import ClassSlot, extract_class_slots, find_matching_slot

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
    gym_account_id: int,
) -> AvailableClasses | None:
    """Probe WodBuster across the week and return the class/time picker set.

    WodBuster's ``ClasesFiltradas`` array is scoped to the day of the
    queried ``ticks``, so a single probe only surfaces the classes
    that run on that one weekday. Classes that only run on a specific
    day (for example a Saturday-only ``Endurance`` session) are
    invisible unless the operator happens to open the form on that
    day. To make the picker day-independent we probe each day of the
    current week (:func:`_current_week_ticks`) and union the results.
    ``LoadClass`` is an ordinary calendar read, not subject to the
    booking-attempt quota, and the form is opened infrequently, so
    the extra reads are cheap.

    Returns ``None`` when the cookie is missing, when WodBuster
    rejects the cookie, or when every daily probe fails. Returns an
    :class:`AvailableClasses` (possibly empty) when at least one probe
    succeeds — the caller inspects ``is_empty`` to decide whether to
    disable the form.
    """
    with get_session() as session:
        cookie_value = store.load(session, gym_account_id)
    if cookie_value is None:
        _log.info("rules.picker.no_cookie", gym_account_id=gym_account_id)
        return None

    class_types: set[str] = set()
    time_slots: set[str] = set()
    days_probed = 0
    for ticks in _current_week_ticks():
        try:
            loaded = client.load_class(cookie_value, ticks)
        except WodBusterAuthError as exc:
            # A rejected cookie fails identically for every day, so
            # stop probing and let the caller render the disabled form.
            _log.warning(
                "rules.picker.auth_error",
                gym_account_id=gym_account_id,
                ticks=ticks,
                error=str(exc),
            )
            return None
        except (WodBusterTransportError, WodBusterProtocolError) as exc:
            # Transient / day-specific failure: skip this day and keep
            # probing the rest of the week.
            _log.warning(
                "rules.picker.upstream_error",
                gym_account_id=gym_account_id,
                ticks=ticks,
                error_type=type(exc).__name__,
                error=str(exc),
            )
            continue

        days_probed += 1
        day = extract_available_classes(loaded.payload)
        class_types.update(day.class_types)
        time_slots.update(day.time_slots)

    if days_probed == 0:
        _log.warning("rules.picker.all_probes_failed", gym_account_id=gym_account_id)
        return None

    result = AvailableClasses(
        class_types=sorted(class_types),
        time_slots=sorted(time_slots),
    )
    _log.info(
        "rules.picker.fetched",
        gym_account_id=gym_account_id,
        class_types=len(result.class_types),
        time_slots=len(result.time_slots),
        days_probed=days_probed,
    )
    return result


@dataclass(frozen=True)
class DateSchedule:
    """Date-scoped probe result for the override form (ADR-0012, Decision 7).

    Distinct from :class:`AvailableClasses`, which unions a whole week
    into two unpaired lists and therefore cannot answer "does this class
    type run at this time on this date" (FR-008).

    ``published`` is ``False`` when the probe succeeded but WodBuster
    carried no class instance for the date. That is not the same as the
    probe returning ``None``, which means it produced no answer at all.
    """

    target_date: date
    published: bool
    slots: list[ClassSlot]

    @property
    def class_types(self) -> list[str]:
        return sorted({slot.nombre for slot in self.slots})

    @property
    def time_slots(self) -> list[str]:
        return sorted({slot.hora_comienzo for slot in self.slots})

    def has(self, class_type: str, class_time: str) -> bool:
        """Whether the exact pair runs on this date.

        Delegates to the matcher the executor uses at trigger time, so a
        validated save and a successful booking agree by construction
        rather than by two implementations staying in sync.
        """
        return (
            find_matching_slot(self.slots, class_type=class_type, class_time=class_time) is not None
        )


def fetch_classes_for_date(
    store: CookieStore,
    client: WodBusterClient,
    gym_account_id: int,
    *,
    target_slot: datetime,
) -> DateSchedule | None:
    """Probe WodBuster for the classes that run on ``target_slot``'s date.

    ``target_slot`` is the UTC class-start instant produced by
    :func:`booking.overrides.effective_slot_for`. Both the calendar day
    the result is labelled with and the ``ticks`` sent upstream are
    derived from it, so the probe reads exactly the day the executor
    will read at trigger time (plan AMB-005). This is deliberately the
    executor's UTC-midnight convention, not the week-scoped picker's
    local-midnight :func:`_current_week_ticks`; the picker is left as it
    is because changing it would alter the rules form.

    One ``load_class`` call, not seven, and no caching: this probe
    exists to be authoritative at the moment it is read.

    Returns ``None`` when the cookie is missing, when WodBuster rejects
    it, or when the call fails.
    """
    target_date = local_date_for_slot(target_slot)
    with get_session() as session:
        cookie_value = store.load(session, gym_account_id)
    if cookie_value is None:
        _log.info(
            "rules.picker.date.no_cookie",
            gym_account_id=gym_account_id,
            target_date=target_date.isoformat(),
        )
        return None

    ticks = midnight_utc_ticks(target_slot)
    try:
        loaded = client.load_class(cookie_value, ticks)
    except WodBusterAuthError as exc:
        _log.warning(
            "rules.picker.date.auth_error",
            gym_account_id=gym_account_id,
            target_date=target_date.isoformat(),
            ticks=ticks,
            error=str(exc),
        )
        return None
    except (WodBusterTransportError, WodBusterProtocolError) as exc:
        _log.warning(
            "rules.picker.date.upstream_error",
            gym_account_id=gym_account_id,
            target_date=target_date.isoformat(),
            ticks=ticks,
            error_type=type(exc).__name__,
            error=str(exc),
        )
        return None

    slots = extract_class_slots(loaded.payload)
    _log.info(
        "rules.picker.date.fetched",
        gym_account_id=gym_account_id,
        target_date=target_date.isoformat(),
        ticks=ticks,
        slots=len(slots),
    )
    return DateSchedule(target_date=target_date, published=bool(slots), slots=slots)


def extract_available_classes(payload: dict[str, Any]) -> AvailableClasses:
    """Extract class types + time slots from a LoadClass payload.

    Unions two sources with slightly different shapes:

    - ``ClasesFiltradas[i]`` — flat rows with ``NombreE`` (name) and
      ``Hora`` (``HH:MM:SS``).
    - ``Data[i]`` — time-slot buckets with a top-level ``Hora`` plus
      a nested ``Valores[j].Valor`` carrying the concrete class
      instance with ``Nombre`` and ``HoraComienzo``. Empirically
      ``Data`` is populated even when ``ClasesFiltradas`` comes back
      empty, so it is the source of truth for the operator's own
      schedule.

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

    for bucket in _iter_dicts(payload.get("Data")):
        # The bucket's own Hora is a reliable time slot.
        hora = bucket.get("Hora")
        if isinstance(hora, str) and len(hora) >= 5 and hora[2] == ":":
            time_slots.add(hora[:5])
        # Concrete class instances live under Valores[j].Valor.
        for entry in _iter_dicts(bucket.get("Valores")):
            valor = entry.get("Valor")
            target = valor if isinstance(valor, dict) else entry
            _accumulate(
                target,
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


def _today_ticks_utc() -> int:
    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _operator_timezone() -> ZoneInfo:
    """Timezone the gym runs on; mirrors the scheduler's rule clock."""
    return ZoneInfo(os.environ.get("WORKER_TIMEZONE", "Europe/Madrid"))


def _current_week_ticks() -> list[int]:
    """Return midnight ticks for each day (Mon..Sun) of the current week.

    The week is anchored to Monday in the operator's timezone (the gym's
    local clock), so "this week" matches the calendar the operator sees
    on WodBuster. Every weekday of the current week is probed exactly
    once and the results are unioned, so the picker offers the sum of
    every distinct class type that runs this week — including days that
    have already passed — rather than a rolling seven-day window.
    """
    tz = _operator_timezone()
    now_local = datetime.now(tz=tz)
    monday_local = (now_local - timedelta(days=now_local.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    return [int((monday_local + timedelta(days=offset)).timestamp()) for offset in range(7)]


__all__ = [
    "AvailableClasses",
    "DateSchedule",
    "extract_available_classes",
    "fetch_available_classes",
    "fetch_classes_for_date",
]
