"""ASGI middleware that maps URL prefix to request language.

Rules:

- Paths under ``/es`` (``/es``, ``/es/``, ``/es/rules``) render in
  Spanish. The middleware strips the prefix from ``scope["path"]``
  before downstream routers see it, so route handlers stay
  language agnostic.
- Any other path renders in English (the default).
- A GET on ``/`` (no prefix) whose ``Accept-Language`` header
  prefers a supported non-default language 302-redirects to that
  language's root (``/es``). This only runs on the exact root and
  only for browsers that did not already type the English root
  explicitly, so it never loops.

The middleware sits inside ``SessionMiddleware`` in
:func:`app.create_app`, so ``request.session`` is still available
downstream but is no longer used for language preference.
"""

from __future__ import annotations

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

        if lang == DEFAULT_LANG and method == "GET" and raw_path == "/":
            # Landing hit with no explicit language choice. Steer
            # browsers that prefer a supported non-default language
            # over to their prefixed root once, so the URL matches
            # the language they will actually see.
            preferred = _preferred_supported_language(Headers(scope=scope))
            if preferred != DEFAULT_LANG:
                await _redirect(send, location=f"/{preferred}")
                return

        set_language(lang)

        if new_path != raw_path:
            # Rewrite the scope so downstream routing matches the
            # language-agnostic route table.
            scope = dict(scope)
            scope["path"] = new_path
            scope["raw_path"] = new_path.encode("utf-8")

        await self.app(scope, receive, send)


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
