"""FastAPI dependencies for authenticated routes (US9.5).

Provides:

- :class:`AuthRedirectRequired`: a custom exception carrying the
  target URL. Registered at app startup with an exception handler
  that renders a plain 302 whose body is empty. The body must not
  leak operator data — CC-011 depends on it.
- :func:`require_session`: the dependency wired into every protected
  route. Reads ``operator_id`` from the session and returns it.
  Missing or invalid session raises :class:`AuthRedirectRequired`.

Idle-timeout enforcement lives in
:class:`auth.session.IdleTimeoutMiddleware`; by the time this
dependency runs, an expired session has already been cleared to an
empty dict, so a missing ``operator_id`` is the sole check needed.
"""

from __future__ import annotations

from datetime import UTC, datetime

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from ..i18n import lang_url
from ..persistence.engine import get_session
from ..persistence.models import OperatorProfile
from ..persistence.users import ban_is_active

# Default provider used for the "not signed in" redirect. Hardcoded
# per the conductor plan; a later story can make it configurable.
DEFAULT_LOGIN_PATH = "/auth/microsoft/login"
SUSPENDED_PATH = "/auth/suspended"


class AuthRedirectRequired(Exception):
    """Raised when an unauthenticated request hits a protected route.

    The app-level exception handler converts this to a 302 response
    whose body is empty, so no operator data can leak through the
    redirect (CC-011).
    """

    def __init__(self, location: str = DEFAULT_LOGIN_PATH) -> None:
        super().__init__(f"authentication required; redirect to {location}")
        self.location = location


def require_session(request: Request) -> int:
    """Return the ``operator_id`` bound to the current session.

    Raises :class:`AuthRedirectRequired` when no session is present or
    when the stored ``operator_id`` is malformed (defensive: the
    callback route is the only writer and always stores an ``int``).
    The redirect target is language-scoped so a Spanish-branch
    visitor lands on ``/es/auth/microsoft/login`` and gets bounced
    back to ``/es`` after signing in.
    """
    operator_id = request.session.get("operator_id")
    if not isinstance(operator_id, int):
        raise AuthRedirectRequired(location=lang_url(DEFAULT_LOGIN_PATH))
    # Enforce ban / deletion immediately on the next request (ADR-0010): a
    # session seated before the admin acted must not keep working.
    state = _operator_access_state(operator_id)
    if state == "banned":
        request.session.clear()
        raise AuthRedirectRequired(location=lang_url(SUSPENDED_PATH))
    if state == "gone":
        request.session.clear()
        raise AuthRedirectRequired(location=lang_url(DEFAULT_LOGIN_PATH))
    return operator_id


def _operator_access_state(operator_id: int) -> str:
    """Return ``"ok"``, ``"banned"``, or ``"gone"`` for the operator."""
    with get_session() as session:
        row = session.execute(
            select(OperatorProfile.banned_until).where(OperatorProfile.id == operator_id)
        ).first()
    if row is None:
        return "gone"
    if ban_is_active(row[0], datetime.now(tz=UTC)):
        return "banned"
    return "ok"


def require_admin(operator_id: int = Depends(require_session)) -> int:
    """Assert the seated operator is an admin; 404 otherwise.

    Admin routes return 404 (not 403) for a non-admin so the admin
    surface is not revealed to regular users (ADR-0010).
    """
    with get_session() as session:
        is_admin = session.scalars(
            select(OperatorProfile.is_admin).where(OperatorProfile.id == operator_id)
        ).first()
    if not is_admin:
        raise HTTPException(status_code=404, detail="not found")
    return operator_id


__all__ = [
    "DEFAULT_LOGIN_PATH",
    "AuthRedirectRequired",
    "require_admin",
    "require_session",
]
