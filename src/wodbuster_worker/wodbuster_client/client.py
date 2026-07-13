"""Sync WodBuster HTTP client (US1.1, US1.2).

Wraps ``httpx.Client`` around the three WodBuster handlers discovered
in Phase 0:

- ``LoadClass.ashx`` — read the operator's calendar view for a week.
- ``Calendario_Inscribir.ashx`` — book a slot.
- ``Calendario_Borrar.ashx`` — cancel a booking.

All three share the same auth model (``.WBAuth`` cookie in a Cookie
header), URL shape (gym subdomain + ``/athlete/handlers/*.ashx``), and
success signal (200 + JSON body). The private
:meth:`_authenticated_get` factors that shared skeleton so the three
public methods stay readable.

Design choices worth calling out:

- **Sync**. The scheduler and validator both run in threaded jobs; the
  extra machinery of ``asyncio`` buys nothing here and would complicate
  the eventual live-contract test.
- **HTTP/1.1** (``http2=False``). Phase 0 confirmed the WodBuster
  endpoints accept HTTP/1.1 and reject HTTP/2 upgrades from some CDN
  paths. Not worth negotiating a feature the server does not need.
- **Cookie per call, not per client**. The ``.WBAuth`` value lives in
  the caller (``CookieStore``); this client is stateless with respect
  to it. That keeps the client cheap to share and keeps tests trivial
  to stub.
- **Base URL derived from ``gym`` and ``base_domain``**. A gym-scoped
  subdomain is the WodBuster convention; we do not expose the full
  URL as a config knob because building it wrong is easy.
- **Typed exceptions** for the three failure modes callers must
  distinguish: transport failure (:class:`WodBusterTransportError`),
  auth rejection (:class:`WodBusterAuthError`), and parse/protocol
  failure (:class:`WodBusterProtocolError`).
- **Booking outcome classifier**. The mutating endpoints return a
  ``Res`` string that the server uses to signal booked / full /
  refused. We map known Spanish values to a small internal
  vocabulary (``granted`` / ``full`` / ``cookie_invalid``) and pass
  everything unknown through as ``unknown`` so the executor can log
  and surface it without crashing. The mapping table is intentionally
  centralised in :func:`classify_res` so adjusting it after a live
  observation is a one-line change.

``connectionId`` is intentionally omitted from the booking calls.
Phase 2a confirmed the parameter is optional and that skipping it
lets us drop the SignalR client from the booking hot path (see
Phase 0 feasibility report §"SignalR is a notification channel").
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import httpx

_CONNECT_TIMEOUT_S = 5.0
_READ_TIMEOUT_S = 15.0
_LOGIN_PATH_MARKER = "/account/login"  # WodBuster redirects unauth'd requests here.


class WodBusterTransportError(Exception):
    """Raised on network-level failure (DNS, TCP, TLS, timeout).

    Distinct from :class:`WodBusterAuthError` because the caller often
    wants to retry a transport failure but not an auth failure.
    """


class WodBusterAuthError(Exception):
    """Raised when the server signals the cookie is invalid or expired.

    Signals observed in Phase 0: a 302 redirect to ``/account/login``,
    a 200 response with HTML content type (the login page rendered
    server-side), or a 401/403 from any handler.
    """


class WodBusterProtocolError(Exception):
    """Raised when a 200 response is received but not parseable as the
    expected JSON shape. This means either the API changed or the
    server is degraded; not something the caller can retry through.
    """


# Vocabulary the executor operates on. Kept as a Literal so mypy
# catches typos at the call site. Values line up with the booking
# terminal-status enum in ``persistence.models`` where they overlap;
# ``unknown`` here is intentionally not in that enum — the executor
# escalates unknown outcomes to ``upstream_unavailable`` after logging
# the raw payload.
BookingOutcomeKind = Literal[
    "granted",
    "full",
    "cookie_invalid",
    "unknown",
]


# Known ``Res`` values observed in Phase 0 / educated guesses from
# WodBuster's Spanish UI vocabulary. Values are case-insensitive:
# :func:`classify_res` lower-cases the input before lookup. Extend the
# table as the live-contract test (US1.T6) surfaces new strings; every
# new entry should carry a short comment sourcing it.
_RES_MAP: dict[str, BookingOutcomeKind] = {
    # Booked successfully.
    "ok": "granted",
    "correcto": "granted",
    "reservada": "granted",
    "inscrita": "granted",
    # Slot has no free places at the moment of the booking call.
    "completa": "full",
    "llena": "full",
    "sinplazas": "full",
    "sin_plazas": "full",
    # Cookie rejected mid-flight (rare — auth failure normally shows
    # up as a redirect handled earlier in :meth:`_authenticated_get`).
    "sinacceso": "cookie_invalid",
    "sin_acceso": "cookie_invalid",
    "sinsession": "cookie_invalid",
    "sin_sesion": "cookie_invalid",
}


def classify_res(value: str | None) -> BookingOutcomeKind:
    """Map a raw WodBuster ``Res`` field to an internal outcome.

    ``None`` and empty strings map to ``unknown`` — the server did
    include a body but no result marker, which the caller should log
    and treat as a soft failure. Case and surrounding whitespace are
    normalised before lookup.
    """
    if value is None:
        return "unknown"
    key = value.strip().lower().replace(" ", "")
    if not key:
        return "unknown"
    return _RES_MAP.get(key, "unknown")


@dataclass(frozen=True)
class LoadClassResponse:
    """Successful parsed result of ``LoadClass.ashx``."""

    status_code: int
    latency_ms: float
    payload: dict[str, Any]


@dataclass(frozen=True)
class BookingActionResponse:
    """Parsed result of a mutating booking or cancel call.

    ``raw_res`` is preserved separately from ``outcome`` so the
    executor can persist the exact server string on
    :class:`BookingOutcome.response_payload` — the classifier's
    ``unknown`` bucket is where we need that trail most.
    """

    status_code: int
    latency_ms: float
    outcome: BookingOutcomeKind
    raw_res: str | None
    payload: dict[str, Any]


class WodBusterClientProtocol(Protocol):
    """Minimal interface :class:`CookieValidator` (and stubs) implement."""

    def load_class(
        self, cookie_value: str, ticks: int
    ) -> LoadClassResponse:  # pragma: no cover - protocol only
        ...


class WodBusterClient:
    """Sync HTTP client for the gym-scoped WodBuster endpoints.

    Construct once at startup and reuse. The underlying ``httpx.Client``
    holds a keep-alive connection pool; tearing it down and rebuilding
    per call would defeat the point.
    """

    __slots__ = ("_base_url", "_client", "_idu")

    def __init__(
        self,
        gym: str,
        idu: str,
        *,
        base_domain: str = "wodbuster.com",
        connect_timeout_s: float = _CONNECT_TIMEOUT_S,
        read_timeout_s: float = _READ_TIMEOUT_S,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not gym:
            raise ValueError("gym must be a non-empty slug")
        if not idu:
            raise ValueError("idu must be a non-empty identifier")
        self._base_url = f"https://{gym}.{base_domain}"
        self._idu = idu
        # Tests inject their own httpx.Client backed by
        # httpx.MockTransport. Production callers rely on the default.
        self._client = http_client or httpx.Client(
            timeout=httpx.Timeout(
                connect=connect_timeout_s,
                read=read_timeout_s,
                write=connect_timeout_s,
                pool=connect_timeout_s,
            ),
            http2=False,
            # Do NOT follow redirects: an unauthenticated request bounces
            # to /account/login.aspx, and we want to surface that as an
            # explicit auth failure rather than silently fetch the HTML
            # login page.
            follow_redirects=False,
            headers={
                "User-Agent": "wodbuster-booking-scheduler/0.1 (+ops)",
                "Accept": "application/json, text/json, */*",
            },
        )

    def close(self) -> None:
        """Release the underlying connection pool."""
        self._client.close()

    # ------------------------------------------------------------------
    # Public endpoints
    # ------------------------------------------------------------------

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        """Fetch the operator's calendar view for the week at ``ticks``.

        A successful call implies the cookie is valid (Phase 0 evidence:
        the server serves authenticated JSON only when ``.WBAuth`` is
        accepted). The validator relies on that implication.

        Raises :class:`WodBusterAuthError` when the server rejects the
        cookie, :class:`WodBusterTransportError` on network failure,
        and :class:`WodBusterProtocolError` on unparseable success.
        """
        status_code, latency_ms, payload = self._authenticated_get(
            path="/athlete/handlers/LoadClass.ashx",
            cookie_value=cookie_value,
            params={"ticks": ticks, "idu": self._idu},
        )
        return LoadClassResponse(
            status_code=status_code,
            latency_ms=latency_ms,
            payload=payload,
        )

    def inscribir(
        self,
        cookie_value: str,
        *,
        class_id: str | int,
        ticks: int,
    ) -> BookingActionResponse:
        """Book the class instance ``class_id`` for the week at ``ticks``.

        Returns the parsed outcome plus the raw ``Res`` string. The
        executor picks the transition (persist row, notify) based on
        ``outcome``. Same auth / transport / protocol error contract
        as :meth:`load_class`.
        """
        return self._booking_action(
            path="/athlete/handlers/Calendario_Inscribir.ashx",
            cookie_value=cookie_value,
            class_id=class_id,
            ticks=ticks,
        )

    def borrar(
        self,
        cookie_value: str,
        *,
        class_id: str | int,
        ticks: int,
    ) -> BookingActionResponse:
        """Cancel the booking on class instance ``class_id`` (FR-014).

        Symmetric with :meth:`inscribir`: same URL shape, same
        ``Res`` classifier. A cancellation the server accepts comes
        back as ``granted`` (poor vocabulary fit, but the outcome
        vocabulary is intentionally tiny; the executor knows the
        action's polarity).
        """
        return self._booking_action(
            path="/athlete/handlers/Calendario_Borrar.ashx",
            cookie_value=cookie_value,
            class_id=class_id,
            ticks=ticks,
        )

    # ------------------------------------------------------------------
    # Shared plumbing
    # ------------------------------------------------------------------

    def _booking_action(
        self,
        *,
        path: str,
        cookie_value: str,
        class_id: str | int,
        ticks: int,
    ) -> BookingActionResponse:
        status_code, latency_ms, payload = self._authenticated_get(
            path=path,
            cookie_value=cookie_value,
            params={
                "id": class_id,
                "ticks": ticks,
                "idu": self._idu,
            },
        )
        raw_res = payload.get("Res")
        raw_res_str = raw_res if isinstance(raw_res, str) else None
        return BookingActionResponse(
            status_code=status_code,
            latency_ms=latency_ms,
            outcome=classify_res(raw_res_str),
            raw_res=raw_res_str,
            payload=payload,
        )

    def _authenticated_get(
        self,
        *,
        path: str,
        cookie_value: str,
        params: dict[str, Any],
    ) -> tuple[int, float, dict[str, Any]]:
        """Issue an authenticated GET and return ``(status, latency_ms, payload)``.

        Every WodBuster handler we call shares the same auth model
        (Cookie header) and success signal (200 + JSON), so this
        helper collects the classification logic. Raises the same
        typed exceptions the public methods document.
        """
        url = f"{self._base_url}{path}"
        # Cache-buster matches the UI's request shape so the request
        # looks identical to a browser call from the CDN's perspective.
        full_params = {**params, "_": int(time.time() * 1000)}
        # Set the Cookie header explicitly rather than using httpx's
        # per-request ``cookies=`` kwarg. The kwarg is deprecated in
        # httpx 0.28+ because its interaction with the client-level
        # cookie jar is ambiguous; a manual header keeps this client
        # stateless with respect to auth material (which is the whole
        # design intent — the caller owns cookies, not us).
        headers = {"Cookie": f".WBAuth={cookie_value}"}

        start = time.perf_counter()
        try:
            response = self._client.get(url, params=full_params, headers=headers)
        except httpx.TransportError as exc:
            raise WodBusterTransportError(str(exc)) from exc
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Redirects to /account/login are the canonical "cookie rejected"
        # signal (Phase 0). We disabled follow_redirects specifically so
        # this branch fires deterministically.
        if response.is_redirect:
            location = response.headers.get("location", "")
            if _LOGIN_PATH_MARKER in location.lower():
                raise WodBusterAuthError(f"redirected to login: {location}")
            raise WodBusterProtocolError(
                f"unexpected redirect {response.status_code} to {location!r}"
            )

        if response.status_code in (401, 403):
            raise WodBusterAuthError(
                f"server returned {response.status_code}"
            )

        if response.status_code != 200:
            raise WodBusterProtocolError(
                f"unexpected status {response.status_code}"
            )

        content_type = response.headers.get("content-type", "").lower()
        # Phase 0 fingerprint of a rejected cookie served as a soft 200:
        # WodBuster renders the login page as HTML with a 200 status.
        # Detect by content type rather than parsing the body.
        if "json" not in content_type:
            raise WodBusterAuthError(
                f"expected JSON, got content-type {content_type!r}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise WodBusterProtocolError(f"invalid JSON body: {exc}") from exc

        if not isinstance(payload, dict):
            raise WodBusterProtocolError(
                f"expected JSON object, got {type(payload).__name__}"
            )

        return response.status_code, elapsed_ms, payload


__all__ = [
    "BookingActionResponse",
    "BookingOutcomeKind",
    "LoadClassResponse",
    "WodBusterAuthError",
    "WodBusterClient",
    "WodBusterClientProtocol",
    "WodBusterProtocolError",
    "WodBusterTransportError",
    "classify_res",
]
