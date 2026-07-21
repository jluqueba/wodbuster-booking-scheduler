"""Manual ad-hoc booking service (US8.1).

A one-off booking for a specific class date + time, triggered from the
web UI (``/book-now`` form) or Telegram (``/bookclass <date> <time>``).
Unlike the scheduler path (:meth:`BookingExecutor.book`), a manual
booking:

- Runs from a request / webhook context, so it must NOT block on the
  US1.5 alignment poll or the US1.7 class-not-visible retry loop. It
  delegates to :meth:`BookingExecutor.book_single_attempt`, which fires
  exactly one attempt.
- Carries no stored :class:`SchedulerRule`; the persisted outcome
  records ``rule_id IS NULL``.
- Enforces the reservation-window precondition itself (FR-019, CC-010):
  it reads ``SegundosHastaPublicacion`` from a single ``LoadClass`` read
  and rejects, WITHOUT issuing any booking / ``inscribir`` call, when the
  window has not opened. A ``LoadClass`` READ is allowed while the window
  is closed; only the mutating booking call is forbidden.
- Resolves the class TYPE from the operator's date + time alone: the
  operator gives no class name, so the service matches whichever class
  runs at ``target_time`` via :func:`find_slot_by_time`.

Timezone: ``target_time`` is the operator's gym-local ``HH:MM`` (the
LoadClass ``HoraComienzo`` field is also gym-local). The service builds
a local aware datetime from ``target_date`` + ``target_time`` in the
operator timezone and converts to UTC for ``target_slot`` (stored UTC),
but hands the raw gym-local ``HH:MM`` string to the executor as
``class_time`` so the slot match lines up with the live payload.

Error surface (mirrors :mod:`cancellation`):

- ``NoCookieError`` — no cookie on file; no upstream call issued.
- ``BookingWindowClosedError`` — window not open (FR-019 / CC-010).
- ``ClassNotVisibleError`` — no class runs at that date + time.
- ``ManualBookingUpstreamError`` — the precondition ``LoadClass`` read
  failed (auth / transport / protocol). No booking attempt made.

A successful delegation returns a :class:`ManualBookingResult` wrapping
the :class:`BookingResult`. The terminal status may still be non-granted
(``full``, or ``cookie_invalid`` if WodBuster rejects the cookie on the
mutating call); callers inspect ``terminal_status`` for the surface copy.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

import structlog

from ..persistence.cookie_store import CookieStore
from ..persistence.engine import get_session
from ..scheduler.rule_jobs import operator_timezone
from ..wodbuster_client.client import (
    BookingActionResponse,
    LoadClassResponse,
    WodBusterAuthError,
    WodBusterProtocolError,
    WodBusterTransportError,
)
from ..wodbuster_client.parsers import (
    extract_class_slots,
    extract_seconds_until_publication,
    find_matching_slot,
    find_slot_by_time,
)
from .executor import BookingExecutor, BookingResult, SessionFactory, _midnight_utc_ticks

_log = structlog.get_logger(__name__)


class ManualBookingError(Exception):
    """Base class for expected manual-booking rejections."""


class NoCookieError(ManualBookingError):
    """No cookie on file; no upstream call was issued."""


class BookingWindowClosedError(ManualBookingError):
    """Target class is not within its booking window (FR-019, CC-010).

    No ``inscribir`` call is issued when this is raised — only the
    read-only ``LoadClass`` probe that surfaced the countdown.
    """

    def __init__(self, seconds_until_open: float) -> None:
        self.seconds_until_open = seconds_until_open
        super().__init__(f"booking window opens in {seconds_until_open:.0f}s")


class ClassNotVisibleError(ManualBookingError):
    """No class runs at the requested date + time on the calendar."""


class ManualBookingUpstreamError(ManualBookingError):
    """The precondition ``LoadClass`` read failed; no booking attempted."""


class ManualBookingClientProtocol(Protocol):
    """WodBuster surface the manual service needs (structural type).

    ``load_class`` for the window precondition; ``inscribir`` because
    the delegated :class:`BookingExecutor` needs the mutating call.
    """

    def load_class(
        self, cookie_value: str, ticks: int
    ) -> LoadClassResponse:  # pragma: no cover - protocol only
        ...

    def inscribir(  # pragma: no cover - protocol only
        self,
        cookie_value: str,
        *,
        class_id: str | int,
        ticks: int,
    ) -> BookingActionResponse: ...


@dataclass(frozen=True)
class ManualBookingResult:
    """Outcome of :meth:`ManualBookingService.book`.

    Wraps the persisted :class:`BookingResult` plus the resolved class
    type and UTC slot so callers can render a surface message without
    re-opening a session. ``terminal_status`` may be non-granted.
    """

    outcome_id: int
    terminal_status: str
    fallback_index: int | None
    class_type: str
    target_slot: datetime


class ManualBookingService:
    """One-off booking service (US8.1).

    Constructs its own single-attempt :class:`BookingExecutor` from the
    injected ``client`` + ``cookie_store`` so the web route and Telegram
    webhook only have to hand over the same two dependencies they use
    for cancellation.
    """

    def __init__(
        self,
        *,
        client: ManualBookingClientProtocol,
        cookie_store: CookieStore,
        session_factory: SessionFactory = get_session,
        operator_idu: str | None = None,
    ) -> None:
        self._client = client
        self._cookie_store = cookie_store
        self._session_factory = session_factory
        self._executor = BookingExecutor(
            client=client,
            session_factory=session_factory,
            cookie_store=cookie_store,
            operator_idu=operator_idu,
        )

    def book(
        self,
        *,
        operator_id: int,
        target_date: date,
        target_time: str,
        class_type: str | None = None,
    ) -> ManualBookingResult:
        """Fire one manual booking for ``target_date`` + ``target_time``.

        When ``class_type`` is given, the service books the class whose
        type matches (case-insensitive) at ``target_time`` — this is how
        the operator disambiguates several classes sharing a start time
        (Cross Training vs Open Endurance at 08:30). When ``class_type``
        is ``None`` the service falls back to whichever class runs at
        that time (first match).

        Returns a :class:`ManualBookingResult` on delegation. Raises a
        :class:`ManualBookingError` subclass for the expected rejection
        modes (no cookie, window closed, class not visible, upstream
        read failure) so the caller can map each to a surface message.
        Raises ``ValueError`` when ``target_time`` is not ``HH:MM``.
        """
        hhmm = _normalize_hhmm(target_time)
        target_slot = self._target_slot(target_date, hhmm)
        ticks = _midnight_utc_ticks(target_slot)

        # Precondition step 1 — cookie. A missing cookie rejects before
        # any WodBuster call (FR-019 spirit: no upstream traffic for a
        # request we already know we cannot fulfil).
        cookie = self._load_cookie(operator_id)
        if cookie is None:
            _log.info("booking.manual.no_cookie", operator_id=operator_id)
            raise NoCookieError("no cookie on file")

        # Precondition step 2 — a single read-only LoadClass probe. This
        # is the ONLY upstream call allowed while the window may be
        # closed; the mutating inscribir happens only after the checks
        # below pass.
        payload = self._load_once(cookie, ticks)

        seconds = extract_seconds_until_publication(payload)
        if seconds is not None and seconds > 0:
            # FR-019 / CC-010: window not open. Reject with no booking
            # call. ``None`` (server did not surface a countdown) is
            # treated as "open" and falls through.
            _log.info(
                "booking.manual.window_closed",
                operator_id=operator_id,
                seconds_until_open=seconds,
            )
            raise BookingWindowClosedError(seconds)

        slots = extract_class_slots(payload)
        if class_type is not None:
            slot = find_matching_slot(slots, class_type=class_type, class_time=hhmm)
        else:
            slot = find_slot_by_time(slots, class_time=hhmm)
        if slot is None:
            _log.info(
                "booking.manual.no_class",
                operator_id=operator_id,
                target_time=hhmm,
                class_type=class_type,
            )
            raise ClassNotVisibleError(f"no class at {hhmm} on {target_date.isoformat()}")

        result: BookingResult = self._executor.book_single_attempt(
            operator_id=operator_id,
            class_type=slot.nombre,
            class_time=hhmm,
            target_slot=target_slot,
            rule_id=None,
        )
        _log.info(
            "booking.manual.delegated",
            operator_id=operator_id,
            class_type=slot.nombre,
            terminal_status=result.terminal_status,
            outcome_id=result.outcome_id,
        )
        return ManualBookingResult(
            outcome_id=result.outcome_id,
            terminal_status=result.terminal_status,
            fallback_index=result.fallback_index,
            class_type=slot.nombre,
            target_slot=target_slot,
        )

    def list_class_types_at(
        self,
        *,
        operator_id: int,
        target_date: date,
        target_time: str,
    ) -> list[str]:
        """Return the distinct class types available at ``target_date`` + time.

        Backs the ``/book-now`` class-type picker: a single read-only
        ``LoadClass`` probe resolves every class instance whose start
        time matches ``target_time`` and returns their sorted, distinct
        type names so the operator can disambiguate collisions (several
        classes at the same hour).

        Degrades gracefully like the rules picker: returns an empty list
        when no cookie is on file, when the probe fails, or when no class
        runs at that time. Raises ``ValueError`` when ``target_time`` is
        not ``HH:MM``.
        """
        hhmm = _normalize_hhmm(target_time)
        target_slot = self._target_slot(target_date, hhmm)
        ticks = _midnight_utc_ticks(target_slot)

        cookie = self._load_cookie(operator_id)
        if cookie is None:
            return []
        try:
            payload = self._load_once(cookie, ticks)
        except ManualBookingUpstreamError:
            return []

        names = {slot.nombre for slot in extract_class_slots(payload) if slot.hora_comienzo == hhmm}
        return sorted(names)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _target_slot(self, target_date: date, hhmm: str) -> datetime:
        """Build the UTC slot from a gym-local date + ``HH:MM``.

        The operator's ``HH:MM`` is interpreted in the gym timezone
        (:func:`operator_timezone`) then converted to UTC, matching how
        ``target_slot`` is stored everywhere else in the codebase.
        """
        hour, minute = (int(part) for part in hhmm.split(":"))
        local = datetime(
            target_date.year,
            target_date.month,
            target_date.day,
            hour,
            minute,
            tzinfo=operator_timezone(),
        )
        return local.astimezone(UTC)

    def _load_cookie(self, operator_id: int) -> str | None:
        with self._session_factory() as session:
            return self._cookie_store.load(session, operator_id)

    def _load_once(self, cookie: str, ticks: int) -> dict[str, Any]:
        try:
            loaded = self._client.load_class(cookie, ticks)
        except WodBusterAuthError as exc:
            raise ManualBookingUpstreamError(f"auth error: {exc}") from exc
        except (WodBusterTransportError, WodBusterProtocolError) as exc:
            raise ManualBookingUpstreamError(f"upstream: {exc}") from exc
        return loaded.payload


def _normalize_hhmm(value: str) -> str:
    """Return ``value`` as zero-padded ``HH:MM`` or raise ``ValueError``.

    Accepts a loosely-formatted ``H:MM`` and normalises it so the slot
    match against the calendar's ``HoraComienzo`` (always ``HH:MM``)
    lines up regardless of how the operator typed the time.
    """
    parsed = datetime.strptime(value.strip(), "%H:%M")
    return parsed.strftime("%H:%M")


__all__ = [
    "BookingWindowClosedError",
    "ClassNotVisibleError",
    "ManualBookingClientProtocol",
    "ManualBookingError",
    "ManualBookingResult",
    "ManualBookingService",
    "ManualBookingUpstreamError",
    "NoCookieError",
]
