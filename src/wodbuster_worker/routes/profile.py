"""Profile view/edit route (User Profile T-UP-005).

Two routes under ``/profile``:

- ``GET  /profile`` render the current profile (display name, short
  name, communication language, current picture) in an edit form.
- ``POST /profile`` validate and persist the editable fields.

The profile row always exists for a signed-in operator (it is seeded
together with the federated identity by the bootstrap command), so this
surface only ever UPDATES; it never creates. Picture UPLOAD is a later
task (T-UP-007, private Blob); this page only displays the current
picture or a neutral placeholder.

Auth-gated; the mutating route is CSRF-protected. All user-facing
strings are localised through the i18n catalog so the page follows the
URL-prefix language like the rest of the app.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_session
from ..i18n import lang_url, t
from ..persistence.engine import get_session
from ..persistence.models import OperatorProfile

router = APIRouter(prefix="/profile", tags=["profile"])
_log = structlog.get_logger(__name__)

# Column caps mirror the model; enforced here so an over-long paste is a
# clean validation flash rather than a database error.
_DISPLAY_NAME_MAX = 200
_SHORT_NAME_MAX = 100
_LANGUAGES = frozenset({"es", "en"})


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates is not configured; wire it in lifespan().")
    assert isinstance(templates, Jinja2Templates)
    return templates


def _redirect_with_flash(message: str, *, kind: str = "info") -> RedirectResponse:
    query = urlencode({"flash": message, "flash_kind": kind})
    return RedirectResponse(url=f"{lang_url('/profile')}?{query}", status_code=303)


def _picture_url(profile_picture_ref: str | None) -> str | None:
    """Return a directly usable image URL, or ``None`` for the placeholder.

    Only absolute ``https`` URLs (provider avatars) render directly today;
    a blob object path needs the proxy/SAS wiring that lands with the
    upload task (T-UP-007), so it falls back to the placeholder for now.
    """
    if profile_picture_ref and profile_picture_ref.startswith("https://"):
        return profile_picture_ref
    return None


def nav_user(request: Request) -> dict[str, Any] | None:
    """Return the signed-in operator's nav identity from the session.

    Reads session-cached fields (seeded at login and refreshed on profile
    save) so the nav avatar needs no per-request database round trip.
    Returns ``None`` for an anonymous request.
    """
    session = request.session
    if not isinstance(session.get("operator_id"), int):
        return None
    display = str(session.get("display_name") or "")
    picture_ref = session.get("profile_picture_ref")
    return {
        "display_name": display,
        "picture_url": _picture_url(picture_ref if isinstance(picture_ref, str) else None),
    }


def register_profile_globals(env: Any) -> None:
    """Attach the nav-user helper to a Jinja2 environment."""
    env.globals["nav_user"] = nav_user


@router.get("", name="profile_view")
def profile_view(
    request: Request,
    operator_id: int = Depends(require_session),
    flash: str | None = None,
    flash_kind: str = "info",
) -> Response:
    """Render the profile edit form."""
    templates = _templates(request)
    with get_session() as session:
        profile = session.get(OperatorProfile, operator_id)
        if profile is None:  # pragma: no cover - session integrity guaranteed
            raise RuntimeError(f"no operator_profile for session operator {operator_id}")
        context = {
            "display_name": profile.display_name,
            "short_name": profile.short_name or "",
            "communication_language": profile.communication_language,
            "picture_url": _picture_url(profile.profile_picture_ref),
        }
    return templates.TemplateResponse(
        request=request,
        name="profile.html",
        context={
            **context,
            "flash": flash,
            "flash_kind": flash_kind if flash_kind in {"info", "warning", "error"} else "info",
            "csrf_token": get_csrf_token(request) or "",
        },
    )


@router.post("", name="profile_save", dependencies=[Depends(verify_csrf)])
def profile_save(
    request: Request,
    display_name: str = Form(...),
    short_name: str = Form(""),
    communication_language: str = Form(...),
    operator_id: int = Depends(require_session),
) -> Response:
    """Validate and persist the editable profile fields."""
    name = display_name.strip()
    short = short_name.strip()
    if not name:
        return _redirect_with_flash(t("profile.flash.name_required"), kind="error")
    if len(name) > _DISPLAY_NAME_MAX or len(short) > _SHORT_NAME_MAX:
        return _redirect_with_flash(t("profile.flash.too_long"), kind="error")
    if communication_language not in _LANGUAGES:
        return _redirect_with_flash(t("profile.flash.bad_language"), kind="error")

    with get_session() as session:
        profile = session.get(OperatorProfile, operator_id)
        if profile is None:  # pragma: no cover - session integrity guaranteed
            raise RuntimeError(f"no operator_profile for session operator {operator_id}")
        profile.display_name = name
        profile.short_name = short or None
        profile.communication_language = communication_language
        session.commit()

    # Keep the nav/greeting in sync within the same session.
    request.session["display_name"] = name
    request.session["short_name"] = short
    _log.info("profile.saved", operator_id=operator_id)
    return _redirect_with_flash(t("profile.flash.saved"), kind="info")


__all__ = ["nav_user", "register_profile_globals", "router"]
