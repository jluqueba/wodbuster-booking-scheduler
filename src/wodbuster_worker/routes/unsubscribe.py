"""Public one-click email unsubscribe (ADR-0011).

``GET /unsubscribe?t=<token>`` verifies the signed token and turns off the
operator's operational email categories (bookings, session alerts). Account
(signup lifecycle) mail is transactional and intentionally never disabled here.
No login: the signed, expiring token is the authorization.
"""

from __future__ import annotations

import structlog
from fastapi import APIRouter, Query, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates

from ..notifications.unsubscribe import read_unsubscribe_token
from ..persistence.engine import get_session
from ..persistence.models import OperatorProfile

router = APIRouter(tags=["unsubscribe"])
_log = structlog.get_logger(__name__)

# Categories an unsubscribe turns off. Transactional account mail is excluded.
_OPERATIONAL = ("bookings", "session_alerts")


def _templates(request: Request) -> Jinja2Templates:
    templates = getattr(request.app.state, "templates", None)
    if templates is None:  # pragma: no cover - misconfiguration
        raise RuntimeError("app.state.templates is not configured; wire it in lifespan().")
    assert isinstance(templates, Jinja2Templates)
    return templates


@router.get("/unsubscribe", name="email_unsubscribe")
def unsubscribe(request: Request, token: str = Query("", alias="t")) -> Response:
    """Disable operational email for the operator encoded in the token."""
    secret = getattr(request.app.state, "email_unsubscribe_secret", None)
    operator_id: int | None = None
    if secret and token:
        operator_id = read_unsubscribe_token(token, secret=secret)

    ok = False
    if operator_id is not None:
        with get_session() as session:
            profile = session.get(OperatorProfile, operator_id)
            if profile is not None:
                prefs = dict(profile.email_preferences or {})
                for category in _OPERATIONAL:
                    prefs[category] = False
                profile.email_preferences = prefs
                session.commit()
                ok = True
        _log.info("email.unsubscribe", operator_id=operator_id, ok=ok)

    templates = _templates(request)
    return templates.TemplateResponse(
        request=request,
        name="unsubscribe.html",
        context={"ok": ok},
        status_code=200 if ok else 400,
    )


__all__ = ["router"]
