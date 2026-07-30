"""Profile view/edit route (User Profile T-UP-005).

Two routes under ``/profile``:

- ``GET  /profile`` render the current profile (display name, short
  name, communication language, current picture) in an edit form.
- ``POST /profile`` validate and persist the editable fields.

The profile row always exists for a signed-in operator (it is seeded
together with the federated identity by the bootstrap command), so this
surface only ever UPDATES; it never creates. The profile picture is the
photo the user already keeps in WodBuster (derived from the active gym's
idu) and is shown READ-ONLY; changing it is done in WodBuster (T-UP-014).

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
from ..gyms.context import get_gym_nav
from ..i18n import lang_url, set_language, t
from ..persistence.engine import get_session
from ..persistence.models import OperatorProfile
from ..wodbuster_client.parsers import wodbuster_avatar_url

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


def _active_avatar_url(request: Request) -> str | None:
    """WodBuster avatar of the active gym, or ``None`` for the placeholder.

    The photo is owned and managed by the user in WodBuster; we only show
    it read-only. Derived from the active gym account's ``idu`` (no upload,
    no storage of our own).
    """
    active = get_gym_nav(request).active
    return wodbuster_avatar_url(active.idu) if active is not None else None


def nav_user(request: Request) -> dict[str, Any] | None:
    """Return the signed-in operator's nav identity.

    The name comes from the session (seeded at login, refreshed on save);
    the avatar is the active gym's WodBuster photo. Returns ``None`` for an
    anonymous request.
    """
    session = request.session
    if not isinstance(session.get("operator_id"), int):
        return None
    return {
        "display_name": str(session.get("display_name") or ""),
        "picture_url": _active_avatar_url(request),
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
            "picture_url": _active_avatar_url(request),
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

    # Keep the nav/greeting and the middleware language in sync within
    # the same session (ADR-0008: refresh-on-edit, no DB on the hot path).
    request.session["display_name"] = name
    request.session["short_name"] = short
    request.session["lang"] = communication_language
    # Redirect in the just-saved language so the flash text and the URL
    # prefix land the user on the new language instead of the old one
    # carried by the current (prefixed) request URL.
    set_language(communication_language)
    _log.info("profile.saved", operator_id=operator_id)
    return _redirect_with_flash(t("profile.flash.saved"), kind="info")


__all__ = ["nav_user", "register_profile_globals", "router"]
