"""HTTP route for the gym switcher.

A single ``.WBAuth`` session authenticates every WodBuster gym the identity
can access, so gyms are discovered automatically (on login and on cookie
paste) rather than managed by hand. The only remaining gym route is the
nav switcher, which sets the gym the web session is acting on.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from ..auth.csrf import verify_csrf
from ..auth.deps import require_session
from ..i18n import lang_url
from ..persistence.engine import get_session
from ..persistence.models import GymAccount
from .context import SESSION_KEY

router = APIRouter(prefix="/gyms", tags=["gyms"])


@router.post("/select", name="gyms_select", dependencies=[Depends(verify_csrf)])
def gyms_select(
    request: Request,
    gym_account_id: int = Form(...),
    next_path: str = Form("/", alias="next"),
    operator_id: int = Depends(require_session),
) -> Response:
    """Set the gym the web session is acting on (approach A switcher).

    Validates that the chosen account is active and owned before storing
    it in the session (SEC-002: an id the caller does not own is a 404 with
    no state change), then returns to the page the switch was made from.
    """
    with get_session() as session:
        owned = session.scalars(
            select(GymAccount.id).where(
                GymAccount.id == gym_account_id,
                GymAccount.user_id == operator_id,
                GymAccount.active.is_(True),
            )
        ).first()
    if owned is None:
        raise HTTPException(status_code=404, detail="gym account not found")
    request.session[SESSION_KEY] = gym_account_id
    # Only same-origin relative paths may be used as the return target.
    target = (
        next_path if next_path.startswith("/") and not next_path.startswith("//") else lang_url("/")
    )
    return RedirectResponse(url=target, status_code=303)


__all__ = ["router"]
