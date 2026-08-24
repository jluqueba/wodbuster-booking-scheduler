"""Booking executor state machine (US1.3, US1.6, US1.7).

Orchestrates one booking attempt for a scheduler rule:

1. Load the operator's cookie. Missing cookie → terminal
   ``cookie_invalid`` with a diagnostic payload.
2. Fetch LoadClass for the target week and try to find the primary
   class instance by ``(class_type, class_time)``. If not visible
   yet, retry every ``retry_interval_s`` up to ``retry_timeout_s``
   (US1.7, FR-010).
3. Fire ``inscribir(primary.id)``. Determine the outcome from the
   operator's enrollment state, not from the ``Res`` string: the
   ``Res`` field and the per-slot ``TipoEstado`` marker both proved
   out of sync with the live API (built from Phase 0 UI guesses that
   were never confirmed against a real response body). The
   authoritative signal is whether the operator now appears in the
   target slot's ``AtletasEntrenando`` list — verified against the
   ``inscribir`` response and, if needed, one confirming ``LoadClass``
   read (:meth:`_resolve_enrollment`):
   - enrolled          → terminal ``granted`` (``granted_fallback_index=0``).
   - not enrolled, full → walk to the second shot if configured.
   - ``cookie_invalid`` (auth ``Res`` or redirect) → terminal, open alert.
   - transport / non-JSON / 5xx → terminal ``upstream_unavailable``.
   When the operator ``idu`` is unwired the legacy ``Res`` classifier
   is used as a fallback.
4. If a second shot is configured and the primary came back
   ``full``, repeat the find + fire cycle for the second shot with
   ``granted_fallback_index=1``. A missing second-shot slot follows
   the same class-not-visible retry policy.
5. Whatever the terminal state, persist the outcome + notification
   outbox rows in a single transaction (see
   :func:`persist_outcome`).

Timing is fully injectable (``sleep``, ``time_source``) so unit
tests can drive the retry loop without wall-clock waits. The
executor is time-of-day agnostic: the scheduler decides when to call
``book(...)``; the executor only knows how to run the attempt.

Rule-model-v2 alignment:

Because the rule now carries at most one "second shot" alternative
(rather than the plan-era 5-preference chain), the fallback walk has
a fixed depth of 2. ``granted_fallback_index`` follows the plan:
``0`` for the primary, ``1`` for the second shot.

Single-day override (ADR-0012 Decision 6):

A day carrying an override prepends a third candidate, so the walk
becomes override target, rule primary, rule second shot. The rule's
own two steps are neither reordered nor re-targeted, and
``granted_fallback_index`` keeps meaning "position in the rule's
chain": ``0`` for the override target and the rule primary alike,
``1`` for the second shot. What distinguishes a substituted booking
from a plain one is the orthogonal ``outcome_source`` column, which
turns to ``override_fallback`` the moment the walk advances past the
override's target.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

import structlog

from ..observability import telemetry
from ..persistence.cookie_store import CookieStore
from ..persistence.models import BookingOutcome, SchedulerRule
from ..scheduler.clock import midnight_utc_ticks
from ..wodbuster_client.client import (
    BookingActionResponse,
    GymAccountLike,
    LoadClassResponse,
    WodBusterAuthError,
    WodBusterClientFactory,
    WodBusterProtocolError,
    WodBusterTransportError,
)
from ..wodbuster_client.parsers import (
    ClassSlot,
    SlotEnrollment,
    extract_class_slots,
    extract_seconds_until_publication,
    find_matching_slot,
    read_target_enrollment,
)
from .outcomes import persist_outcome
from .overrides import OverridePlan, effective_slot_for
from .vacation import find_covering_window

_log = structlog.get_logger(__name__)


class _BookingClientProtocol(Protocol):
    """Booking-time client surface (structural type).

    Superset of :class:`WodBusterClientProtocol` — adds the mutating
    ``inscribir`` method the executor needs. Kept private because
    only the executor cares about the combined surface.
    """

    def load_class(
        self, cookie_value: str, ticks: int
    ) -> LoadClassResponse:  # pragma: no cover - protocol only
        ...

    def inscribir(
        self,
        cookie_value: str,
        *,
        class_id: str | int,
        ticks: int,
    ) -> BookingActionResponse:  # pragma: no cover - protocol only
        ...


# Session factory type. Mirrors the shape used by the dispatcher and
# heartbeat wiring (persistence.engine.get_session).
SessionFactory = Callable[[], AbstractContextManager[Any]]


@dataclass(frozen=True)
class BookingResult:
    """Outcome of :meth:`BookingExecutor.book`.

    Wraps the persisted :class:`BookingOutcome` id so callers do not
    have to re-open a session to check what happened. The
    ``fallback_index`` is filled only on ``granted``.
    """

    outcome_id: int
    terminal_status: str
    fallback_index: int | None


class BookingExecutor:
    """One instance per app. Thread-safe as long as the injected
    client and session factory are."""

    def __init__(
        self,
        *,
        client: _BookingClientProtocol,
        session_factory: SessionFactory,
        cookie_store: CookieStore,
        operator_idu: str | None = None,
        retry_interval_s: float = 0.25,
        retry_timeout_s: float = 120.0,
        alignment_poll_interval_s: float = 0.2,
        alignment_threshold_s: float = 0.25,
        alignment_deadline_s: float = 60.0,
        sleep: Callable[[float], None] = time.sleep,
        time_source: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._session_factory = session_factory
        self._cookie_store = cookie_store
        # Operator's WodBuster ``idu`` (Phase 0). Used to confirm a
        # booking by locating the operator in the target slot's
        # ``AtletasEntrenando`` list. ``None`` (unwired) falls back to
        # the legacy ``Res`` classifier in :meth:`_attempt`.
        self._operator_idu = operator_idu
        # Slot-appearance retry cadence. The target class only becomes
        # bookable the instant WodBuster publishes the week, so a poll
        # that lands a hair before publication must re-check quickly (a
        # sub-second interval) rather than sleep for seconds and cede
        # the first slots to manual users.
        self._retry_interval_s = retry_interval_s
        self._retry_timeout_s = retry_timeout_s
        # US1.5 countdown alignment. Poll LoadClass, backing off from
        # coarse to fine steps, until ``SegundosHastaPublicacion`` drops
        # to ``<= threshold`` (a small sub-second value so we release at
        # ~publication rather than up to a full second early), capped by
        # ``deadline`` seconds since alignment started. The scheduler
        # wrapper fires the job ~30s before the window (see
        # ``rule_jobs.register_rule_job``) so the poll loop has time to
        # warm the connection pool and sync against WodBuster's clock
        # instead of relying on the operator's wall time.
        self._alignment_poll_interval_s = alignment_poll_interval_s
        self._alignment_threshold_s = alignment_threshold_s
        self._alignment_deadline_s = alignment_deadline_s
        self._sleep = sleep
        self._time_source = time_source

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def book(
        self,
        *,
        rule: SchedulerRule,
        target_slot: datetime,
        override: OverridePlan | None = None,
    ) -> BookingResult:
        """Attempt to book ``rule`` for the class starting at ``target_slot``.

        ``override`` is the single-day plan the scheduler read for this
        date (ADR-0012); ``None`` is the ordinary case and takes the
        exact code path it took before the feature existed.

        Returns the persisted :class:`BookingResult`. Never raises for
        expected failure modes (missing cookie, full class, unknown
        Res); those all funnel through ``persist_outcome``. Reraises
        unexpected exceptions after logging so the scheduler can
        surface them.
        """
        # Wall-clock timing for the US2.6 booking-latency histogram.
        # Recorded on every exit path via the finally block so
        # skipped / cookie-invalid / granted / full etc. all show up
        # in the metric with their terminal_status attribute.
        start = time.monotonic()
        result: BookingResult | None = None
        try:
            result = self._book_inner(rule=rule, target_slot=target_slot, override=override)
            return result
        finally:
            elapsed_ms = (time.monotonic() - start) * 1000.0
            try:
                telemetry.booking_attempt_latency_ms().record(
                    elapsed_ms,
                    {
                        "terminal_status": (result.terminal_status if result else "unknown"),
                    },
                )
            except Exception:  # pragma: no cover - metric emit must not raise
                _log.exception("booking.metric.record_failed")

    def _book_inner(
        self,
        *,
        rule: SchedulerRule,
        target_slot: datetime,
        override: OverridePlan | None = None,
    ) -> BookingResult:
        """Body of :meth:`book`; wrapped by ``book`` for latency timing."""
        if target_slot.tzinfo is None:
            raise ValueError("target_slot must be timezone-aware")

        ticks = midnight_utc_ticks(target_slot)
        # The override is prepended to the rule's own walk; a run without
        # one yields the same two candidates the executor had before
        # ADR-0012, so the attempt sequence is byte-for-byte unchanged.
        chain = _build_chain(rule=rule, target_slot=target_slot, override=override)
        _log.info(
            "booking.start",
            rule_id=rule.id,
            gym_account_id=rule.gym_account_id,
            target_slot=target_slot.isoformat(),
            ticks=ticks,
            override_id=override.override_id if override is not None else None,
            candidates=len(chain),
        )

        # US7.2 skip guard: if the target class sits inside an open
        # vacation window for the operator, do not attempt the
        # booking. Persist a ``skipped`` terminal so the history
        # page still reports the run. Vacation wins over any override
        # (FR-029), so this stays the first exit and stays sourced to
        # the rule.
        vacation = self._vacation_covering(rule.gym_account_id, target_slot)
        if vacation is not None:
            return self._persist_terminal(
                rule=rule,
                target_class=rule.class_type,
                target_slot=target_slot,
                terminal_status="skipped",
                fallback_index=None,
                response=f"vacation window #{vacation.id}",
                telegram_text=self._render_vacation_skip_text(rule, target_slot),
                outcome_source="rule",
            )

        # Skip exit (FR-011, FR-013, INV-007). Deliberately second, so
        # vacation keeps precedence and stays sourced to the rule. From
        # here on there is no cookie load, no alignment poll, no schedule
        # read and no candidate: omitting means omitting, under every
        # condition, including a bookable rule class. The terminal
        # ``skipped`` row is what lets the history screen report that the
        # executor ran and chose to book nothing.
        if override is not None and override.skip_day:
            return self._persist_terminal(
                rule=rule,
                target_class=rule.class_type,
                target_slot=target_slot,
                terminal_status="skipped",
                fallback_index=None,
                response=f"override #{override.override_id} skips this day",
                telegram_text=self._render_override_skip_text(rule, target_slot),
                outcome_source="override_skip",
            )

        cookie = self._load_cookie(rule.gym_account_id)
        if cookie is None:
            return self._persist_terminal(
                rule=rule,
                target_class=chain[0].class_type,
                target_slot=chain[0].target_slot,
                terminal_status="cookie_invalid",
                fallback_index=None,
                response="no cookie on file",
                telegram_text=self._render_no_cookie_text(rule, target_slot),
                outcome_source=chain[0].outcome_source,
            )

        # US1.5: align on WodBuster's own ``SegundosHastaPublicacion``
        # countdown so the booking fires the instant the window opens
        # server-side, not when our wall clock says ``booking_opens_at``.
        # Also pre-warms the httpx keep-alive pool for the ``inscribir``
        # call that follows.
        self._align_to_publication(cookie=cookie, ticks=ticks, rule_id=int(rule.id))

        return self._walk_chain(rule=rule, chain=chain, cookie=cookie)

    def _walk_chain(
        self,
        *,
        rule: SchedulerRule,
        chain: list[_Candidate],
        cookie: str,
    ) -> BookingResult:
        """Contest each candidate in order until one produces a terminal row.

        Advancing to the next candidate happens in exactly two cases:

        - the candidate came back ``full``, which is the pre-override
          walk's own second-shot condition; and
        - the candidate is the override's target and its class never
          appeared on the schedule (FR-012). That branch is scoped to the
          override's target alone: on the rule's own candidates a
          ``class_not_visible`` stays terminal exactly as it does today.

        Anything else (``granted``, ``cookie_invalid``,
        ``upstream_unavailable``) is terminal wherever it happens: a bad
        cookie or a broken upstream will not behave differently one
        candidate later.
        """
        # Only set when the chain opens with an override target, so a row
        # sourced to ``override_fallback`` always carries what the user
        # asked for and why it could not be honoured (FR-015, INV-008).
        requested_class = chain[0].class_type if chain[0].is_override_target else None
        requested_time = chain[0].class_time if chain[0].is_override_target else None
        fallback_reason: str | None = None

        for position, candidate in enumerate(chain):
            is_last = position == len(chain) - 1
            result = self._attempt(
                rule=rule,
                target_slot=candidate.target_slot,
                ticks=midnight_utc_ticks(candidate.target_slot),
                cookie=cookie,
                class_type=candidate.class_type,
                class_time=candidate.class_time,
                fallback_index=candidate.fallback_index,
            )
            if not is_last and (
                not result.done
                or (candidate.is_override_target and result.terminal_status == "class_not_visible")
            ):
                if candidate.is_override_target:
                    fallback_reason = result.terminal_status
                continue

            is_fallback = candidate.outcome_source == "override_fallback"
            return self._persist_terminal(
                **result.payload,
                outcome_source=candidate.outcome_source,
                requested_class=requested_class if is_fallback else None,
                requested_time=requested_time if is_fallback else None,
                fallback_reason=fallback_reason if is_fallback else None,
            )

        raise AssertionError("attempt chain is never empty")  # pragma: no cover

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _load_cookie(self, gym_account_id: int) -> str | None:
        with self._session_factory() as session:
            return self._cookie_store.load(session, gym_account_id)

    def _vacation_covering(self, gym_account_id: int, target_slot: datetime) -> Any:
        """Return the open vacation window covering ``target_slot`` or ``None``.

        Read-only lookup; opens a short-lived session so the guard
        adds one round-trip rather than holding a connection across
        the whole booking attempt.
        """
        with self._session_factory() as session:
            return find_covering_window(
                session,
                gym_account_id=gym_account_id,
                target_slot=target_slot,
            )

    def _align_to_publication(self, *, cookie: str, ticks: int, rule_id: int) -> None:
        """Poll ``LoadClass`` until the server reports the window is open.

        Reads ``SegundosHastaPublicacion`` off each response and sleeps
        (coarse steps far out, fine steps near the window) until the
        value drops to ``<= alignment_threshold_s`` (default 0.25s, so
        we release at ~publication rather than up to a second early).
        Also serves as the pre-warm tick: the first
        request pays the TCP + TLS handshake cost so the ``inscribir``
        call rides on a warm keep-alive socket.

        Safety net: the loop bails out after ``alignment_deadline_s``
        seconds regardless of the countdown value so a stuck
        publication clock never blocks the executor. Missing or
        malformed countdown fields fall through immediately — the
        legacy code path (fire at ``booking_opens_at`` wall-clock
        time) still holds.

        Upstream errors are logged and swallowed. If the connection
        is broken, the primary ``inscribir`` attempt below will
        surface the failure through the existing terminal-status
        machinery.
        """
        start = self._time_source()
        deadline = start + self._alignment_deadline_s
        polls = 0
        while True:
            try:
                response = self._client.load_class(cookie, ticks)
            except (WodBusterAuthError, WodBusterTransportError, WodBusterProtocolError):
                # Let the primary attempt below surface the real
                # terminal status; alignment does not persist outcomes.
                _log.warning("booking.align.upstream_error", rule_id=rule_id, polls=polls)
                return
            polls += 1
            countdown = extract_seconds_until_publication(response.payload)
            if countdown is None:
                _log.info(
                    "booking.align.countdown_absent",
                    rule_id=rule_id,
                    polls=polls,
                )
                return
            if countdown <= self._alignment_threshold_s:
                _log.info(
                    "booking.align.aligned",
                    rule_id=rule_id,
                    polls=polls,
                    countdown=countdown,
                )
                return
            now = self._time_source()
            if now >= deadline:
                _log.warning(
                    "booking.align.deadline_hit",
                    rule_id=rule_id,
                    polls=polls,
                    countdown=countdown,
                )
                return
            # Adaptive back-off: sleep roughly half the remaining
            # countdown so we take coarse steps while publication is far
            # off and fine steps (down to the poll-interval floor) as it
            # nears. This lands the final poll within one poll interval
            # of publication instead of up to a full second early,
            # without polling LoadClass hundreds of times across the
            # pre-warm lead. Never sleep past the deadline.
            remaining = countdown - self._alignment_threshold_s
            sleep_for = min(
                max(self._alignment_poll_interval_s, remaining / 2.0),
                max(0.0, deadline - now),
            )
            self._sleep(sleep_for)

    def _attempt(
        self,
        *,
        rule: SchedulerRule,
        target_slot: datetime,
        ticks: int,
        cookie: str,
        class_type: str,
        class_time: str,
        fallback_index: int,
    ) -> _AttemptResult:
        """Try to book a single class instance with retry-on-not-visible."""
        slot = self._find_slot_with_retry(
            cookie=cookie, ticks=ticks, class_type=class_type, class_time=class_time
        )
        if slot is None:
            return _AttemptResult.done_terminal(
                rule=rule,
                target_class=class_type,
                target_slot=target_slot,
                terminal_status="class_not_visible",
                fallback_index=None,
                response=(
                    f"class {class_type!r} at {class_time} not visible after "
                    f"{self._retry_timeout_s:.0f}s"
                ),
                telegram_text=self._render_class_not_visible_text(
                    rule, class_type, class_time, target_slot
                ),
            )

        try:
            response = self._client.inscribir(cookie, class_id=slot.id, ticks=ticks)
        except WodBusterAuthError as exc:
            return _AttemptResult.done_terminal(
                rule=rule,
                target_class=class_type,
                target_slot=target_slot,
                terminal_status="cookie_invalid",
                fallback_index=None,
                response=f"auth error: {exc}",
                telegram_text=self._render_cookie_invalid_text(rule, target_slot),
            )
        except (WodBusterTransportError, WodBusterProtocolError) as exc:
            return _AttemptResult.done_terminal(
                rule=rule,
                target_class=class_type,
                target_slot=target_slot,
                terminal_status="upstream_unavailable",
                fallback_index=None,
                response=f"upstream: {exc}",
                telegram_text=self._render_upstream_text(rule, class_type, target_slot),
            )

        outcome = response.outcome
        raw_payload = _short_payload(response.payload, response.raw_res)

        # Local builders so the enrollment-based and Res-fallback paths
        # below share one definition of each terminal shape.
        def _granted(resp_text: str) -> _AttemptResult:
            return _AttemptResult.done_terminal(
                rule=rule,
                target_class=class_type,
                target_slot=target_slot,
                terminal_status="granted",
                fallback_index=fallback_index,
                response=resp_text,
                telegram_text=self._render_granted_text(
                    rule, class_type, class_time, target_slot, fallback_index
                ),
            )

        def _full() -> _AttemptResult:
            # Not yet terminal — caller may walk to the second shot.
            return _AttemptResult.full(
                rule=rule,
                target_class=class_type,
                target_slot=target_slot,
                fallback_index=None,
                response=raw_payload,
                telegram_text=self._render_full_text(rule, class_type, class_time, target_slot),
            )

        def _upstream() -> _AttemptResult:
            return _AttemptResult.done_terminal(
                rule=rule,
                target_class=class_type,
                target_slot=target_slot,
                terminal_status="upstream_unavailable",
                fallback_index=None,
                response=raw_payload,
                telegram_text=self._render_upstream_text(rule, class_type, target_slot),
            )

        # A Res-signalled auth failure stays terminal regardless of the
        # enrollment probe below.
        if outcome == "cookie_invalid":
            return _AttemptResult.done_terminal(
                rule=rule,
                target_class=class_type,
                target_slot=target_slot,
                terminal_status="cookie_invalid",
                fallback_index=None,
                response=raw_payload,
                telegram_text=self._render_cookie_invalid_text(rule, target_slot),
            )

        # Authoritative success signal: the ``Res`` field and the
        # ``TipoEstado`` slot vocabulary both proved out of sync with the
        # live API, so classify from whether the operator now appears in
        # the booked slot's ``AtletasEntrenando`` list.
        enrollment = self._resolve_enrollment(
            cookie=cookie, ticks=ticks, slot_id=slot.id, first_payload=response.payload
        )
        if enrollment is not None and enrollment.found:
            if enrollment.enrolled:
                return _granted(
                    f"{raw_payload} enrolled=confirmed "
                    f"occupied={enrollment.occupied}/{enrollment.capacity}"
                )
            if enrollment.is_full:
                return _full()
            # Slot present, operator not enrolled, and it is not a
            # capacity miss: the booking did not take.
            return _upstream()

        # Verification unavailable (idu unwired or the confirming read
        # failed): fall back to the legacy ``Res`` classifier.
        if outcome == "granted":
            return _granted(raw_payload)
        if outcome == "full":
            return _full()
        # "unknown" — escalate to upstream_unavailable but keep the
        # raw Res so we can extend the classifier post-hoc.
        return _upstream()

    def _resolve_enrollment(
        self,
        *,
        cookie: str,
        ticks: int,
        slot_id: int,
        first_payload: dict[str, Any],
    ) -> SlotEnrollment | None:
        """Confirm whether the operator is enrolled in ``slot_id``.

        Returns ``None`` when verification is impossible (no ``idu``
        configured, or the confirming read failed), signalling the
        caller to fall back to the ``Res`` classifier.

        The ``inscribir`` response mirrors ``LoadClass`` and is checked
        first; if it does not already show the operator enrolled we
        issue one confirming ``LoadClass`` read — the exact method Phase
        0 used to verify a booking end-to-end — in case the mutating
        response predates the enrollment write.
        """
        if not self._operator_idu:
            return None
        first = read_target_enrollment(
            first_payload, slot_id=slot_id, operator_idu=self._operator_idu
        )
        if first.found and first.enrolled:
            return first
        try:
            loaded = self._client.load_class(cookie, ticks)
        except (WodBusterAuthError, WodBusterTransportError, WodBusterProtocolError):
            # Confirming read unavailable: use whatever the inscribir
            # response already told us (may be "not found").
            return first if first.found else None
        return read_target_enrollment(
            loaded.payload, slot_id=slot_id, operator_idu=self._operator_idu
        )

    def _find_slot_with_retry(
        self,
        *,
        cookie: str,
        ticks: int,
        class_type: str,
        class_time: str,
    ) -> ClassSlot | None:
        """Poll LoadClass until the target slot appears or timeout hits."""
        deadline = self._time_source() + self._retry_timeout_s
        while True:
            try:
                loaded = self._client.load_class(cookie, ticks)
            except WodBusterAuthError:
                # Auth failure mid-loop: don't spin — surface upstream
                # so the caller records cookie_invalid via _attempt's
                # inscribir call (which will also see the auth error).
                return None
            except (WodBusterTransportError, WodBusterProtocolError):
                # Transient — try again on the next tick unless we
                # already ran out of budget. Fall through to the
                # sleep + retry check below.
                pass
            else:
                slots = extract_class_slots(loaded.payload)
                match = find_matching_slot(slots, class_type=class_type, class_time=class_time)
                if match is not None:
                    return match

            # Not found this tick. Retry only if we still have time.
            if self._time_source() + self._retry_interval_s > deadline:
                return None
            self._sleep(self._retry_interval_s)

    def _persist_terminal(
        self,
        *,
        rule: SchedulerRule,
        target_class: str,
        target_slot: datetime,
        terminal_status: str,
        fallback_index: int | None,
        response: str | None,
        telegram_text: str,
        outcome_source: str = "rule",
        requested_class: str | None = None,
        requested_time: str | None = None,
        fallback_reason: str | None = None,
    ) -> BookingResult:
        """Open a session, persist outcome + outbox rows, commit."""
        with self._session_factory() as session:
            outcome: BookingOutcome = persist_outcome(
                session,
                gym_account_id=rule.gym_account_id,
                rule_id=rule.id,
                target_class=target_class,
                target_slot=target_slot,
                terminal_status=terminal_status,
                outcome_source=outcome_source,
                granted_fallback_index=fallback_index,
                response_payload=response,
                telegram_text=telegram_text,
                requested_class=requested_class,
                requested_time=requested_time,
                fallback_reason=fallback_reason,
            )
            session.commit()
            _log.info(
                "booking.persist",
                rule_id=rule.id,
                gym_account_id=rule.gym_account_id,
                terminal_status=terminal_status,
                outcome_source=outcome_source,
                fallback_index=fallback_index,
                outcome_id=outcome.id,
            )
            return BookingResult(
                outcome_id=int(outcome.id),
                terminal_status=terminal_status,
                fallback_index=fallback_index,
            )

    # -- Copy rendering ------------------------------------------------
    # Kept as private methods so subclasses can override for
    # localisation without touching the state machine.

    def _render_granted_text(
        self,
        rule: SchedulerRule,
        class_type: str,
        class_time: str,
        target_slot: datetime,
        fallback_index: int,
    ) -> str:
        tag = "primary" if fallback_index == 0 else "second shot"
        return f"Booked {class_type} at {class_time} for {_format_slot(target_slot)} ({tag})."

    def _render_full_text(
        self,
        rule: SchedulerRule,
        class_type: str,
        class_time: str,
        target_slot: datetime,
    ) -> str:
        return (
            f"Could not book {class_type} at {class_time} for "
            f"{_format_slot(target_slot)}: class was full."
        )

    def _render_class_not_visible_text(
        self,
        rule: SchedulerRule,
        class_type: str,
        class_time: str,
        target_slot: datetime,
    ) -> str:
        return (
            f"Could not book {class_type} at {class_time} for "
            f"{_format_slot(target_slot)}: class did not appear on the "
            "schedule within the retry window."
        )

    def _render_cookie_invalid_text(
        self,
        rule: SchedulerRule,
        target_slot: datetime,
    ) -> str:
        return (
            f"Booking for {_format_slot(target_slot)} skipped: WodBuster "
            "cookie is invalid. Paste a fresh cookie to resume bookings."
        )

    def _render_no_cookie_text(
        self,
        rule: SchedulerRule,
        target_slot: datetime,
    ) -> str:
        return (
            f"Booking for {_format_slot(target_slot)} skipped: no "
            "WodBuster cookie on file. Paste one on the Cookie page."
        )

    def _render_vacation_skip_text(
        self,
        rule: SchedulerRule,
        target_slot: datetime,
    ) -> str:
        return (
            f"Booking for {_format_slot(target_slot)} skipped: vacation mode is on for this date."
        )

    def _render_override_skip_text(
        self,
        rule: SchedulerRule,
        target_slot: datetime,
    ) -> str:
        return (
            f"Booking for {_format_slot(target_slot)} skipped: you marked "
            "this day as skipped. No class was contested."
        )

    def _render_upstream_text(
        self,
        rule: SchedulerRule,
        class_type: str,
        target_slot: datetime,
    ) -> str:
        return (
            f"Booking for {class_type} on {_format_slot(target_slot)} failed: "
            "WodBuster response was unexpected. Check the worker logs."
        )


# ----------------------------------------------------------------------
# Result envelopes
# ----------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    """One step of the trigger-time attempt chain (ADR-0012 Decision 6).

    ``outcome_source`` is fixed when the chain is built rather than
    derived at persist time: every candidate after the override's own
    target is a fallback whatever terminal status it ends on.
    """

    class_type: str
    class_time: str
    target_slot: datetime
    fallback_index: int
    outcome_source: str
    is_override_target: bool = False


def _build_chain(
    *,
    rule: SchedulerRule,
    target_slot: datetime,
    override: OverridePlan | None,
) -> list[_Candidate]:
    """Return the ordered candidates for one booking run.

    The order is override target, rule primary, rule second shot
    (FR-012). The override is prepended; the rule's own walk is neither
    reordered nor re-targeted, so an ``override`` of ``None`` yields the
    exact two candidates the executor contested before ADR-0012.
    """
    contests_override = override is not None and not override.skip_day and override.changes_target
    rule_source = "override_fallback" if contests_override else "rule"
    # Where the rule's own class sits on this day. It only diverges from
    # the slot the scheduler resolved when the override moved the time.
    rule_slot = target_slot
    chain: list[_Candidate] = []

    if contests_override and override is not None:
        chain.append(
            _Candidate(
                class_type=override.effective_class_type(rule),
                class_time=override.effective_class_time(rule),
                target_slot=target_slot,
                fallback_index=0,
                outcome_source="override",
                is_override_target=True,
            )
        )
        if override.class_time is not None:
            rule_slot = effective_slot_for(rule, override.target_date, str(rule.class_time))

    chain.append(
        _Candidate(
            class_type=str(rule.class_type),
            class_time=str(rule.class_time),
            target_slot=rule_slot,
            fallback_index=0,
            outcome_source=rule_source,
        )
    )

    # ``suppress_second_shot`` drops the last candidate for this date
    # only; the rule's own configuration is never read back or written
    # (FR-017, FR-018).
    suppressed = override is not None and override.suppress_second_shot
    if rule.second_shot_class_type and rule.second_shot_class_time and not suppressed:
        chain.append(
            _Candidate(
                class_type=str(rule.second_shot_class_type),
                class_time=str(rule.second_shot_class_time),
                # Unchanged from the pre-override walk: the second shot's
                # row carries the primary candidate's slot.
                target_slot=rule_slot,
                fallback_index=1,
                outcome_source=rule_source,
            )
        )
    return chain


@dataclass(frozen=True)
class _AttemptResult:
    """Internal helper: outcome of one primary/second-shot attempt.

    Either terminal (``done=True``) with ``payload`` describing the
    row to persist, OR non-terminal-full (``done=False``) with two
    payloads: ``payload`` for the "full" case, and ``full_payload``
    reused when the caller decides to persist without walking to a
    second shot.
    """

    done: bool
    payload: dict[str, Any]
    full_payload: dict[str, Any]

    @property
    def terminal_status(self) -> str:
        return str(self.payload["terminal_status"])

    @classmethod
    def done_terminal(cls, **payload: Any) -> _AttemptResult:
        return cls(done=True, payload=payload, full_payload=payload)

    @classmethod
    def full(cls, **payload: Any) -> _AttemptResult:
        payload = {**payload, "terminal_status": "full"}
        return cls(done=False, payload=payload, full_payload=payload)


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------


def _short_payload(payload: dict[str, Any], raw_res: str | None) -> str:
    """Return a compact, human-readable string for ``response_payload``.

    The full LoadClass-flavoured body is ~4 KB; store only the ``Res``
    and a truncated key snapshot. Post-mortem readers can pull the
    full body from the App Insights request log if we ever need it.
    """
    keys = ",".join(sorted(payload.keys())[:8])
    return f"Res={raw_res!r} keys=[{keys}...]"


def _format_slot(target_slot: datetime) -> str:
    """Short human-readable slot label for notification copy.

    Rendered in the operator's local timezone (``WORKER_TIMEZONE``,
    default ``Europe/Madrid``) so booking messages read in the same
    wall-clock the operator books against. Imported lazily to avoid a
    circular import (``rule_jobs`` imports this module).
    """
    from ..scheduler.rule_jobs import operator_timezone

    return target_slot.astimezone(operator_timezone()).strftime("%a %d %b %H:%M %Z")


class BookingExecutorProvider:
    """Resolve a per-gym-account :class:`BookingExecutor` (ADR-0007, P2).

    A booking rule targets a specific gym account, so its attempt must run
    against that gym's subdomain (client) with that gym's ``idu``. This
    provider caches one executor per ``(gym_slug, idu)`` pair, each bound
    to the client the :class:`WodBusterClientFactory` returns for that gym
    account. The shared ``cookie_store`` still resolves the per-gym-account
    cookie at call time (keyed by ``gym_account_id``), so credentials never
    cross gym boundaries.

    Thread-safe for the scheduler's use: executors are built once per gym
    account and reused; the cache is only ever populated, never mutated in
    a way that races a concurrent read for a different key.
    """

    __slots__ = ("_cache", "_client_factory", "_cookie_store", "_session_factory")

    def __init__(
        self,
        *,
        client_factory: WodBusterClientFactory,
        session_factory: SessionFactory,
        cookie_store: CookieStore,
    ) -> None:
        self._client_factory = client_factory
        self._session_factory = session_factory
        self._cookie_store = cookie_store
        self._cache: dict[tuple[str, str], BookingExecutor] = {}

    def for_gym_account(self, gym_account: GymAccountLike) -> BookingExecutor:
        """Return the executor bound to ``gym_account``, building on miss."""
        key = (gym_account.gym_slug, gym_account.idu)
        executor = self._cache.get(key)
        if executor is None:
            executor = BookingExecutor(
                client=self._client_factory.get(gym_account),
                session_factory=self._session_factory,
                cookie_store=self._cookie_store,
                operator_idu=gym_account.idu,
            )
            self._cache[key] = executor
        return executor


__all__ = [
    "BookingExecutor",
    "BookingExecutorProvider",
    "BookingResult",
    "SessionFactory",
]
