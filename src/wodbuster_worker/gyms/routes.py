"""HTTP routes for the gym-account management flow (US multi-gym P1).

Two routes under ``/gyms``:

- ``GET  /gyms`` list the user's gym accounts + the add form (only gyms
  from the curated allow-list the user does not already own are offered).
- ``POST /gyms`` add a gym: validate the slug against the allow-list
  (SEC-001) BEFORE constructing any client, then validate the pasted
  cookie and discover the operator's own idu, and persist atomically.

Both routes are auth-gated; the mutating route is CSRF-protected. All
user-facing strings (page copy and flash messages) are localised through
the i18n catalog (``t(...)``), so the page follows the URL-prefix
language like the rest of the app.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

import structlog
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_owned_gym_account, require_session
from ..i18n import lang_url, t
from ..persistence.engine import get_session
from ..persistence.models import GymAccount
from ..wodbuster_client.client import (
    WodBusterAuthError,
    WodBusterProtocolError,
    WodBusterTransportError,
)
from .service import (
    GymAlreadyExistsError,
    add_gym_account,
    refresh_gym_cookie,
    set_gym_account_active,
)

router = APIRouter(prefix="/gyms", tags=["gyms"])
_log = structlog.get_logger(__name__)


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates is not configured; wire it in lifespan().")
    assert isinstance(templates, Jinja2Templates)
    return templates


def _redirect_with_flash(message: str, *, kind: str = "info") -> RedirectResponse:
    query = urlencode({"flash": message, "flash_kind": kind})
    return RedirectResponse(url=f"{lang_url('/gyms')}?{query}", status_code=303)


@router.get("", name="gyms_list")
def gyms_list(
    request: Request,
    operator_id: int = Depends(require_session),
    flash: str | None = None,
    flash_kind: str = "info",
) -> Response:
    """Render the user's gym accounts and the add-gym form."""
    templates = _templates(request)
    settings = request.app.state.settings
    with get_session() as session:
        accounts = (
            session.execute(
                select(GymAccount).where(GymAccount.user_id == operator_id).order_by(GymAccount.id)
            )
            .scalars()
            .all()
        )
        rows: list[dict[str, Any]] = [
            {
                "id": a.id,
                "gym_slug": a.gym_slug,
                "display_name": a.display_name,
                "active": a.active,
            }
            for a in accounts
        ]
    owned = {r["gym_slug"] for r in rows}
    addable = sorted(settings.known_gym_slugs() - owned)
    return templates.TemplateResponse(
        request=request,
        name="gyms/list.html",
        context={
            "gyms": rows,
            "addable": addable,
            "flash": flash,
            "flash_kind": flash_kind if flash_kind in {"info", "warning", "error"} else "info",
            "csrf_token": get_csrf_token(request) or "",
        },
    )


@router.post("", name="gyms_add", dependencies=[Depends(verify_csrf)])
def gyms_add(
    request: Request,
    gym_slug: str = Form(...),
    cookie_value: str = Form(...),
    display_name: str = Form(""),
    operator_id: int = Depends(require_session),
) -> Response:
    """Validate and add a gym account for the current user."""
    settings = request.app.state.settings
    cookie_store = getattr(request.app.state, "cookie_store", None)
    factory = getattr(request.app.state, "gym_discovery_factory", None)
    if cookie_store is None or factory is None:
        return _redirect_with_flash(t("gyms.flash.unavailable"), kind="error")

    # SEC-001: validate the slug against the allow-list BEFORE building any
    # client or URL from it, so a crafted value can never redirect the
    # pasted cookie to another host.
    try:
        slug = settings.validate_gym_slug(gym_slug)
    except ValueError:
        return _redirect_with_flash(t("gyms.flash.not_allowlisted"), kind="error")

    client = factory(slug)
    with get_session() as session:
        try:
            add_gym_account(
                session,
                user_id=operator_id,
                gym_slug=slug,
                cookie_value=cookie_value,
                settings=settings,
                cookie_store=cookie_store,
                client=client,
                display_name=display_name.strip() or None,
            )
        except GymAlreadyExistsError:
            return _redirect_with_flash(t("gyms.flash.duplicate"), kind="warning")
        except WodBusterAuthError:
            return _redirect_with_flash(t("gyms.flash.cookie_rejected"), kind="error")
        except (WodBusterProtocolError, WodBusterTransportError):
            _log.warning("gyms.add.discovery_failed", gym_slug=slug)
            return _redirect_with_flash(t("gyms.flash.unreachable"), kind="error")
        except ValueError:
            return _redirect_with_flash(t("gyms.flash.not_allowlisted"), kind="error")
        session.commit()

    return _redirect_with_flash(t("gyms.flash.added", slug=slug), kind="info")


@router.post(
    "/{gym_account_id}/cookie",
    name="gyms_refresh_cookie",
    dependencies=[Depends(verify_csrf)],
)
def gyms_refresh_cookie(
    request: Request,
    cookie_value: str = Form(...),
    gym_account_id: int = Depends(require_owned_gym_account),
) -> Response:
    """Re-validate and store a fresh cookie for one gym account (FR-005).

    Scoped to the single ``gym_account_id`` in the path (ownership
    enforced by :func:`require_owned_gym_account`, SEC-002), so
    refreshing one gym's cookie never touches another's.
    """
    settings = request.app.state.settings
    cookie_store = getattr(request.app.state, "cookie_store", None)
    factory = getattr(request.app.state, "gym_discovery_factory", None)
    if cookie_store is None or factory is None:
        return _redirect_with_flash(t("gyms.flash.unavailable"), kind="error")

    del settings  # not needed here; slug comes from the stored account
    with get_session() as session:
        account = session.get(GymAccount, gym_account_id)
        if account is None:  # pragma: no cover - authz guarantees existence
            return _redirect_with_flash(t("gyms.flash.gone"), kind="error")
        client = factory(account.gym_slug)
        try:
            refresh_gym_cookie(
                session,
                gym_account_id=gym_account_id,
                cookie_value=cookie_value,
                cookie_store=cookie_store,
                client=client,
            )
        except ValueError:
            return _redirect_with_flash(t("gyms.flash.cookie_blank"), kind="error")
        except WodBusterAuthError:
            return _redirect_with_flash(t("gyms.flash.cookie_rejected"), kind="error")
        except (WodBusterProtocolError, WodBusterTransportError):
            _log.warning("gyms.refresh.discovery_failed", gym_account_id=gym_account_id)
            return _redirect_with_flash(t("gyms.flash.unreachable"), kind="error")
        session.commit()

    return _redirect_with_flash(t("gyms.flash.cookie_refreshed"), kind="info")


@router.post(
    "/{gym_account_id}/deactivate",
    name="gyms_deactivate",
    dependencies=[Depends(verify_csrf)],
)
def gyms_deactivate(
    request: Request,
    gym_account_id: int = Depends(require_owned_gym_account),
) -> Response:
    """Deactivate a gym account (FR-006): stops future bookings/heartbeats."""
    del request  # only the authz dependency is needed
    with get_session() as session:
        set_gym_account_active(session, gym_account_id, active=False)
        session.commit()
    return _redirect_with_flash(t("gyms.flash.deactivated"), kind="info")


@router.post(
    "/{gym_account_id}/reactivate",
    name="gyms_reactivate",
    dependencies=[Depends(verify_csrf)],
)
def gyms_reactivate(
    request: Request,
    gym_account_id: int = Depends(require_owned_gym_account),
) -> Response:
    """Reactivate a gym account (FR-006): restores bookings and heartbeats."""
    del request  # only the authz dependency is needed
    with get_session() as session:
        set_gym_account_active(session, gym_account_id, active=True)
        session.commit()
    return _redirect_with_flash(t("gyms.flash.reactivated"), kind="info")


__all__ = ["router"]
