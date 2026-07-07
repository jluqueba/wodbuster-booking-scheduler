"""Sync WodBuster HTTP client (US1.1 slice for US-003).

Wraps ``httpx.Client`` around the three WodBuster handlers discovered
in Phase 0 (``LoadClass.ashx``, ``Calendario_Inscribir.ashx``,
``Calendario_Borrar.ashx``). Only :meth:`load_class` is implemented in
this slice because that is what :class:`CookieValidator` needs. The two
mutating methods land with US-001.

Design choices worth calling out:

- **Sync**. The scheduler and validator both run in threaded jobs; the
  extra machinery of ``asyncio`` buys nothing here and would complicate
  the eventual live-contract test.
- **HTTP/1.1** (``http2=False``). Phase 0 confirmed the WodBuster
  endpoints accept HTTP/1.1 and reject HTTP/2 upgrades from some CDN
  paths. Not worth negotiating a feature the server does not need.
- **Cookie per call, not per client**. The ``.WBAuth`` value lives in
  the caller (``CookieStore``); this client is stateless with respect
  to it. That keeps the client cheap to share across operators (there
  is only one operator today but the type stays honest) and keeps
  tests trivial to stub.
- **Base URL derived from ``gym`` and ``base_url``**. A gym-scoped
  subdomain is the WodBuster convention (``antworktrainingcenter.wodbuster.com``);
  we do not expose the full URL as a config knob because building it
  wrong is easy and only the gym slug varies.
- **Typed exceptions** for the three failure modes callers must
  distinguish: transport failure (:class:`WodBusterTransportError`),
  auth rejection (:class:`WodBusterAuthError`) which surfaces as a
  redirect to ``/account/login.aspx`` or an HTML response, and
  parse/protocol failure (:class:`WodBusterProtocolError`) for a 200
  response that is not the expected JSON shape. Everything else
  bubbles up unchanged.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

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


@dataclass(frozen=True)
class LoadClassResponse:
    """Successful parsed result of ``LoadClass.ashx``.

    Only the fields the validator (and later the scheduler) actually
    read are surfaced; the full JSON body stays available in ``payload``
    for callers that need it.
    """

    status_code: int
    latency_ms: float
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

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        """Fetch the operator's calendar view for the week at ``ticks``.

        A successful call implies the cookie is valid (Phase 0 evidence:
        the server serves authenticated JSON only when ``.WBAuth`` is
        accepted). The validator relies on that implication.

        Raises :class:`WodBusterAuthError` when the server rejects the
        cookie, :class:`WodBusterTransportError` on network failure,
        and :class:`WodBusterProtocolError` on unparseable success.
        """
        url = f"{self._base_url}/athlete/handlers/LoadClass.ashx"
        params: dict[str, Any] = {
            "ticks": ticks,
            "idu": self._idu,
            "_": int(time.time() * 1000),  # cache-buster matches the UI's shape
        }
        # Set the Cookie header explicitly rather than using httpx's
        # per-request ``cookies=`` kwarg. The kwarg is deprecated in
        # httpx 0.28+ because its interaction with the client-level
        # cookie jar is ambiguous; a manual header keeps this client
        # stateless with respect to auth material (which is the whole
        # design intent — the caller owns cookies, not us).
        headers = {"Cookie": f".WBAuth={cookie_value}"}

        start = time.perf_counter()
        try:
            response = self._client.get(url, params=params, headers=headers)
        except httpx.TransportError as exc:
            # Covers timeout, connect refused, DNS, TLS. httpx.TimeoutException
            # is a subclass; catching TransportError is sufficient.
            raise WodBusterTransportError(str(exc)) from exc
        elapsed_ms = (time.perf_counter() - start) * 1000

        # Redirects to /account/login are the canonical "cookie rejected"
        # signal (Phase 0). We disabled follow_redirects specifically so
        # this branch fires deterministically.
        if response.is_redirect:
            location = response.headers.get("location", "")
            if _LOGIN_PATH_MARKER in location.lower():
                raise WodBusterAuthError(
                    f"redirected to login: {location}"
                )
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
        # We detect it by content type rather than parsing the body.
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

        return LoadClassResponse(
            status_code=response.status_code,
            latency_ms=elapsed_ms,
            payload=payload,
        )


__all__ = [
    "LoadClassResponse",
    "WodBusterAuthError",
    "WodBusterClient",
    "WodBusterClientProtocol",
    "WodBusterProtocolError",
    "WodBusterTransportError",
]
