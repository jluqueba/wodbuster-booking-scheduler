"""ASGI middleware that resolves the request language (ADR-0008).

Precedence (highest first):

- An explicit ``/es`` prefix (``/es``, ``/es/``, ``/es/rules``) renders
  Spanish for that request. The middleware strips the prefix from
  ``scope["path"]`` before downstream routers see it, so route handlers
  stay language agnostic.
- Otherwise, a signed-in user's stored ``communication_language`` (cached
  on the session at login, refreshed on profile edit) decides. The value
  is read only; the URL is never rewritten for a signed-in user.
- Otherwise (anonymous), a GET on ``/`` whose ``Accept-Language`` header
  prefers a supported non-default language 302-redirects once to that
  language's root (``/es``); it never loops.
- Otherwise English (the default).

Runs inside ``SessionMiddleware`` (see :func:`app.create_app`), so the
signed-in ``operator_id`` and cached ``lang`` are available on
``scope["session"]``.
"""

from __future__ import annotations

from collections.abc import MutableMapping

from starlette.datastructures import Headers
from starlette.types import ASGIApp, Receive, Scope, Send

from . import DEFAULT_LANG, SUPPORTED_LANGUAGES, normalize_language, set_language

# Prefixes we treat as language selectors. English is intentionally
# prefix-free — it is the "no prefix" default.
_PREFIXES: tuple[tuple[str, str], ...] = tuple(
    (f"/{code}", code) for code in SUPPORTED_LANGUAGES if code != DEFAULT_LANG
)


class LanguageMiddleware:
    """Bind the current language to the contextvar for the request scope."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        raw_path: str = scope.get("path", "/")
        method: str = scope.get("method", "GET")
        lang, new_path = _match_prefix(raw_path)

        if lang != DEFAULT_LANG:
            # 1. An explicit URL prefix (``/es``) wins for this request.
            set_language(lang)
        elif _is_signed_in(scope.get("session")):
            # 2. No explicit prefix: a signed-in user's stored language is
            # authoritative (ADR-0008). Read-only; the URL is never rewritten.
            set_language(_session_language(scope.get("session")))
        else:
            # 3. Anonymous: steer a browser that prefers a supported
            # non-default language to its prefixed root once, 4. else default.
            if method == "GET" and raw_path == "/":
                preferred = _preferred_supported_language(Headers(scope=scope))
                if preferred != DEFAULT_LANG:
                    await _redirect(send, location=f"/{preferred}")
                    return
            set_language(DEFAULT_LANG)

        if new_path != raw_path:
            # Rewrite the scope so downstream routing matches the
            # language-agnostic route table.
            scope = dict(scope)
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode("utf-8")

        await self.app(scope, receive, send)


def _is_signed_in(session: object) -> bool:
    """True when the ASGI session carries an authenticated ``operator_id``."""
    return isinstance(session, MutableMapping) and isinstance(session.get("operator_id"), int)


def _session_language(session: object) -> str:
    """Return the signed-in user's cached language (validated enum) or default.

    The value is seeded at login and refreshed on profile-language edit, so
    the hot path never hits the database. Anything not in
    :data:`SUPPORTED_LANGUAGES` collapses to :data:`DEFAULT_LANG` (SEC-009).
    """
    lang = session.get("lang") if isinstance(session, MutableMapping) else None
    return lang if isinstance(lang, str) and lang in SUPPORTED_LANGUAGES else DEFAULT_LANG


def _match_prefix(path: str) -> tuple[str, str]:
    """Return ``(language, stripped_path)`` for the request path.

    ``/es`` and ``/es/`` both collapse to ``/`` so the root
    handler serves the Spanish landing without a special case.
    Longer paths like ``/es/rules`` become ``/rules``.
    """
    for prefix, lang in _PREFIXES:
        if path == prefix or path == f"{prefix}/":
            return lang, "/"
        if path.startswith(f"{prefix}/"):
            return lang, path[len(prefix) :]
    return DEFAULT_LANG, path


def _preferred_supported_language(headers: Headers) -> str:
    """Return the browser's most-preferred supported language.

    Walks the ``Accept-Language`` header in declared order (real
    q-value negotiation is overkill for two locales) and returns
    the first supported code. Falls back to :data:`DEFAULT_LANG`
    when nothing matches.
    """
    header = headers.get("accept-language", "")
    if not header:
        return DEFAULT_LANG
    for token in header.split(","):
        candidate = token.split(";", 1)[0].strip()
        if not candidate:
            continue
        normalised = normalize_language(candidate)
        if normalised in SUPPORTED_LANGUAGES and normalised != DEFAULT_LANG:
            return normalised
        if normalised == DEFAULT_LANG:
            # Browser explicitly prefers English over anything that
            # might follow — stop walking so we do not redirect.
            return DEFAULT_LANG
    return DEFAULT_LANG


async def _redirect(send: Send, *, location: str) -> None:
    """Emit a 302 to ``location`` without touching downstream apps."""
    await send(
        {
            "type": "http.response.start",
            "status": 302,
            "headers": [
                (b"location", location.encode("ascii")),
                (b"content-length", b"0"),
                (b"cache-control", b"no-store"),
            ],
        }
    )
    await send({"type": "http.response.body", "body": b"", "more_body": False})


__all__ = ["LanguageMiddleware"]
