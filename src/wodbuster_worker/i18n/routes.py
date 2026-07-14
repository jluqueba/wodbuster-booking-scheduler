"""Language-switch route (US i18n).

One POST endpoint. The request may carry a ``Referer`` header;
we bounce back to it when it's a safe same-origin URL so the
operator lands on the same page they clicked from. Falls back to
``/`` when the referrer is absent or cross-origin.
"""

from __future__ import annotations

from urllib.parse import urlparse

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from ..auth.csrf import verify_csrf
from ..i18n import normalize_language

router = APIRouter(tags=["settings"])


@router.post(
    "/settings/language",
    name="settings_language",
    dependencies=[Depends(verify_csrf)],
)
def set_language_route(
    request: Request,
    lang: str = Form(...),
) -> Response:
    """Persist the language choice on the session and redirect back."""
    normalised = normalize_language(lang)
    request.session["lang"] = normalised
    target = _safe_referer(request) or "/"
    return RedirectResponse(url=target, status_code=303)


def _safe_referer(request: Request) -> str | None:
    """Return the ``Referer`` when it points at this app; else ``None``.

    Guards against an attacker abusing the language switch as an
    open redirect: only same-scheme + same-host URLs are honoured.
    Relative paths (rare but possible) pass through unchanged.
    """
    header = request.headers.get("referer")
    if not header:
        return None
    parsed = urlparse(header)
    if not parsed.netloc:
        # Relative path — safe to bounce back to.
        return header
    request_host = request.url.hostname
    if request_host and parsed.hostname == request_host:
        # Preserve the path + query; drop scheme/host so the
        # response stays same-origin.
        return parsed.path + (f"?{parsed.query}" if parsed.query else "")
    return None


__all__ = ["router"]
