"""Admin-only user management (ADR-0010).

One page (``/admin/users``) shows pending signups (approve / reject) and the
active users (ban for a period, ban indefinitely, un-ban, or delete). Every
route is gated by :func:`require_admin`, which returns 404 for a non-admin so
the surface is not revealed. Destructive actions never touch the acting admin
or another admin, so the platform can never be left without an administrator.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select

from ..auth.csrf import get_csrf_token, verify_csrf
from ..auth.deps import require_admin
from ..i18n import lang_url
from ..persistence.engine import get_session
from ..persistence.models import FederatedIdentity, OperatorProfile
from ..persistence.users import INDEFINITE_BAN, ban_is_active

router = APIRouter(prefix="/admin", tags=["admin"])

# Ban durations offered in the UI, mapped to a delta from now.
_BAN_DURATIONS = {
    "1d": timedelta(days=1),
    "7d": timedelta(days=7),
    "30d": timedelta(days=30),
}


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates is not configured; wire it in lifespan().")
    assert isinstance(templates, Jinja2Templates)
    return templates


@router.get("/users", name="admin_users")
def admin_users(request: Request, admin_id: int = Depends(require_admin)) -> Response:
    """Render pending signups and the active-user management list."""
    now = datetime.now(tz=UTC)
    templates = _templates(request)
    with get_session() as session:
        pending_rows = session.execute(
            select(
                OperatorProfile.id,
                OperatorProfile.display_name,
                OperatorProfile.email,
                FederatedIdentity.provider,
            )
            .join(FederatedIdentity, FederatedIdentity.operator_id == OperatorProfile.id)
            .where(OperatorProfile.status == "pending")
            .order_by(OperatorProfile.created_at)
        ).all()
        active_rows = session.execute(
            select(
                OperatorProfile.id,
                OperatorProfile.display_name,
                OperatorProfile.email,
                OperatorProfile.is_admin,
                OperatorProfile.banned_until,
            )
            .where(OperatorProfile.status == "active")
            .order_by(OperatorProfile.display_name)
        ).all()

    pending = [
        {"id": r.id, "display_name": r.display_name, "email": r.email, "provider": r.provider}
        for r in pending_rows
    ]
    users: list[dict[str, Any]] = []
    for r in active_rows:
        banned = ban_is_active(r.banned_until, now)
        users.append(
            {
                "id": r.id,
                "display_name": r.display_name,
                "email": r.email,
                "is_admin": r.is_admin,
                "is_self": r.id == admin_id,
                "banned": banned,
                "banned_indefinitely": banned and r.banned_until == INDEFINITE_BAN,
                "banned_until": r.banned_until if banned else None,
            }
        )
    return templates.TemplateResponse(
        request=request,
        name="admin/users.html",
        context={"pending": pending, "users": users, "csrf_token": get_csrf_token(request) or ""},
    )


def _set_status_if_pending(target_id: int, status: str) -> None:
    with get_session() as session:
        profile = session.get(OperatorProfile, target_id)
        if profile is not None and profile.status == "pending":
            profile.status = status
            session.commit()


def _ban_target(target_id: int, admin_id: int, duration: str) -> None:
    """Ban a non-admin active user; guarded against self and other admins."""
    if duration != "indefinite" and duration not in _BAN_DURATIONS:
        return
    with get_session() as session:
        profile = session.get(OperatorProfile, target_id)
        if profile is None or profile.id == admin_id or profile.is_admin:
            return
        if duration == "indefinite":
            profile.banned_until = INDEFINITE_BAN
        else:
            profile.banned_until = datetime.now(tz=UTC) + _BAN_DURATIONS[duration]
        session.commit()


def _unban_target(target_id: int) -> None:
    with get_session() as session:
        profile = session.get(OperatorProfile, target_id)
        if profile is not None and not profile.is_admin:
            profile.banned_until = None
            session.commit()


def _delete_target(target_id: int, admin_id: int) -> None:
    """Hard-delete a non-admin user; cascades to all their data (ADR-0010)."""
    with get_session() as session:
        profile = session.get(OperatorProfile, target_id)
        if profile is None or profile.id == admin_id or profile.is_admin:
            return
        session.delete(profile)
        session.commit()


def _back() -> RedirectResponse:
    return RedirectResponse(url=lang_url("/admin/users"), status_code=303)


@router.post(
    "/users/{target_id}/approve", name="admin_approve", dependencies=[Depends(verify_csrf)]
)
def admin_approve(target_id: int, admin_id: int = Depends(require_admin)) -> Response:
    """Approve a pending signup: the user gains normal access."""
    del admin_id
    _set_status_if_pending(target_id, "active")
    return _back()


@router.post("/users/{target_id}/reject", name="admin_reject", dependencies=[Depends(verify_csrf)])
def admin_reject(target_id: int, admin_id: int = Depends(require_admin)) -> Response:
    """Reject a pending signup: the user is denied until they re-request."""
    del admin_id
    _set_status_if_pending(target_id, "rejected")
    return _back()


@router.post("/users/{target_id}/ban", name="admin_ban", dependencies=[Depends(verify_csrf)])
def admin_ban(
    target_id: int,
    duration: str = Form(...),
    admin_id: int = Depends(require_admin),
) -> Response:
    """Ban a user for a period or indefinitely."""
    _ban_target(target_id, admin_id, duration)
    return _back()


@router.post("/users/{target_id}/unban", name="admin_unban", dependencies=[Depends(verify_csrf)])
def admin_unban(target_id: int, admin_id: int = Depends(require_admin)) -> Response:
    """Lift a user's ban."""
    del admin_id
    _unban_target(target_id)
    return _back()


@router.post("/users/{target_id}/delete", name="admin_delete", dependencies=[Depends(verify_csrf)])
def admin_delete(target_id: int, admin_id: int = Depends(require_admin)) -> Response:
    """Permanently delete a user and all of their data."""
    _delete_target(target_id, admin_id)
    return _back()


__all__ = ["router"]
