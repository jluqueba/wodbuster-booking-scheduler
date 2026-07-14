"""ASGI middleware that binds the request's language to the context.

Order of precedence:

1. ``session["lang"]`` — set by the operator via the language
   selector; wins over any header because the operator's explicit
   choice must survive across browsers.
2. ``Accept-Language`` header — the browser's own preference,
   used on the first request before the operator has picked.
3. :data:`DEFAULT_LANG` — the ultimate fallback.

Runs on every request AFTER Starlette's ``SessionMiddleware``
(which populates ``request.session``); order is enforced by the
middleware stack construction in :func:`app.create_app`.
"""

from __future__ import annotations

from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
from starlette.types import ASGIApp

from . import normalize_language, set_language


class LanguageMiddleware(BaseHTTPMiddleware):
    """Bind the current language to the contextvar for the request scope."""

    def __init__(self, app: ASGIApp) -> None:
        super().__init__(app)

    async def dispatch(
        self,
        request: Request,
        call_next: Any,
    ) -> Response:
        lang = _resolve_language(request)
        set_language(lang)
        response: Response = await call_next(request)
        return response


def _resolve_language(request: Request) -> str:
    """Pick the language for ``request`` (session → header → default)."""
    session = getattr(request, "session", None)
    if session is not None:
        stored = session.get("lang")
        if stored:
            return normalize_language(stored)
    header = request.headers.get("accept-language", "")
    if header:
        # ``Accept-Language`` can carry weighted alternatives
        # (``es-ES,es;q=0.9,en;q=0.8``); the first token wins after
        # normalisation. Real quality-value negotiation is overkill
        # for two supported languages.
        first = header.split(",")[0].strip()
        return normalize_language(first)
    return normalize_language(None)


__all__ = ["LanguageMiddleware"]
