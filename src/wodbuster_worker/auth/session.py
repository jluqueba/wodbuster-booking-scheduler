"""Session middleware wiring for US-009.

Two pieces:

- :func:`build_session_middleware` returns Starlette's
  :class:`SessionMiddleware` configured with the encrypted cookie key
  pulled from Key Vault (``session-encryption-secret``). The cookie is
  ``HttpOnly``, ``Secure``, ``SameSite=Lax``, browser-lifetime only,
  path ``/``.
- :class:`IdleTimeoutMiddleware` wraps ``SessionMiddleware`` and
  enforces the idle and absolute session lifetimes. It refreshes
  ``last_seen_at`` on every request that has an active session and
  clears the session (in-place) when the idle or absolute deadline
  passes. Redirection to the sign-in flow is left to the
  ``require_session`` dependency so that public routes (``/health``,
  ``/auth/*``) never receive a stale-session redirect.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from starlette.datastructures import MutableHeaders
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..config import Settings
from ..security.keyvault import Secrets

# Cookie name is stable and documented; changing it invalidates every
# outstanding session. Kept as a module constant so the CSRF module can
# share the ``wodbuster_`` prefix.
SESSION_COOKIE_NAME = "wodbuster_session"

# Session-state keys touched by this module. Kept as constants so the
# routes / deps / CSRF module can reference them without magic strings.
LAST_SEEN_KEY = "last_seen_at"
CREATED_AT_KEY = "created_at"


def build_session_middleware(settings: Settings, secrets: Secrets) -> Middleware:
    """Build the :class:`SessionMiddleware` with the KV-sourced key.

    Fails loudly when ``session_encryption_secret`` is missing, since a
    silent fallback to a dev-only key would open the door to trivial
    session-cookie forgery in prod. The check runs at app startup, not
    at request time.
    """
    if not secrets.session_encryption_secret:
        raise RuntimeError(
            "session_encryption_secret is not configured. In prod it must "
            "resolve from Key Vault (secret name 'session-encryption-secret'); "
            "in local mode set SESSION_ENCRYPTION_SECRET in .env."
        )
    return Middleware(
        SessionMiddleware,
        secret_key=secrets.session_encryption_secret,
        session_cookie=SESSION_COOKIE_NAME,
        # Browser-lifetime session cookie. ``max_age=None`` tells
        # Starlette to omit the Max-Age/Expires attributes so the
        # cookie dies on browser close; the absolute cap is enforced
        # server-side by :class:`IdleTimeoutMiddleware`.
        max_age=None,
        same_site="lax",
        # ``https_only`` corresponds to the ``Secure`` cookie flag.
        # Local dev over http still works because Starlette skips
        # setting the flag when the request is not TLS; but in prod
        # (behind Container Apps HTTPS ingress) the flag is set.
        https_only=True,
        path="/",
    )


class IdleTimeoutMiddleware:
    """Enforce idle + absolute session lifetimes.

    Runs *outside* :class:`SessionMiddleware` in the stack, which means
    it sees ``request.session`` populated on incoming and its mutations
    flushed on outgoing. The middleware mutates the session in-place;
    ``SessionMiddleware`` picks up ``scope["session"]`` changes when it
    writes the response cookie.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        idle_minutes: int,
        absolute_hours: int,
    ) -> None:
        self.app = app
        self._idle = timedelta(minutes=idle_minutes)
        self._absolute = timedelta(hours=absolute_hours)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        session = scope.get("session")
        if isinstance(session, dict) and session:
            self._enforce(session)

        await self.app(scope, receive, send)

    def _enforce(self, session: dict[str, object]) -> None:
        """Mutate the session dict in place per idle/absolute policy."""
        now = datetime.now(UTC)
        created_iso = session.get(CREATED_AT_KEY)
        last_seen_iso = session.get(LAST_SEEN_KEY)

        # No timestamps yet — treat as fresh anonymous state. This
        # branch also covers pre-callback sessions that only hold the
        # ``oauth_state_*`` value; leaving them alone lets the callback
        # complete.
        if not isinstance(created_iso, str) or not isinstance(last_seen_iso, str):
            return

        try:
            created_at = datetime.fromisoformat(created_iso)
            last_seen_at = datetime.fromisoformat(last_seen_iso)
        except ValueError:
            # Corrupt session — safest is to clear and let the next
            # request start over.
            session.clear()
            return

        # Absolute cap and idle timeout both simply invalidate the
        # session. ``require_session`` sees the empty session and
        # redirects; public routes see the cleared state and proceed.
        if now - created_at >= self._absolute or now - last_seen_at >= self._idle:
            session.clear()
            return

        session[LAST_SEEN_KEY] = now.isoformat()


def touch_session(session: dict[str, object]) -> None:
    """Stamp ``created_at`` + ``last_seen_at`` on a newly authenticated session.

    Called from the OAuth callback right after ``operator_id`` is
    written, so the idle middleware has non-``None`` anchors from the
    first authenticated request onward.
    """
    now_iso = datetime.now(UTC).isoformat()
    session[CREATED_AT_KEY] = now_iso
    session[LAST_SEEN_KEY] = now_iso


# ---------------------------------------------------------------------------
# Response-header helper
# ---------------------------------------------------------------------------
#
# Kept here so both the CSRF module and the auth routes have a single
# place to write cookies with matching attributes (path, secure, etc.).


def set_response_cookie(
    headers: MutableHeaders,
    *,
    name: str,
    value: str,
    http_only: bool = True,
    same_site: str = "lax",
) -> None:
    """Append a ``Set-Cookie`` header with the project-wide defaults.

    ``Secure`` is unconditionally set; the app runs behind HTTPS in
    prod. Local dev over ``http://localhost`` still lets the browser
    accept the cookie because Chrome and Firefox exempt ``localhost``.
    """
    attrs = [f"{name}={value}", "Path=/", "Secure", f"SameSite={same_site}"]
    if http_only:
        attrs.append("HttpOnly")
    headers.append("set-cookie", "; ".join(attrs))


def _fmt_message_header(message: Message, name: bytes, value: bytes) -> None:
    """Append a header to an ASGI ``http.response.start`` message.

    Not part of the public API; kept for future middleware wiring.
    """
    headers = list(message.get("headers", []))
    headers.append((name, value))
    message["headers"] = headers
