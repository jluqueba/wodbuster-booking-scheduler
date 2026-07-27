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

from fastapi import Depends, HTTPException, Request
from sqlalchemy import select

from ..i18n import lang_url
from ..persistence.engine import get_session
from ..persistence.models import GymAccount

# Default provider used for the "not signed in" redirect. Hardcoded
# per the conductor plan; a later story can make it configurable.
DEFAULT_LOGIN_PATH = "/auth/microsoft/login"


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
    return operator_id


def require_owned_gym_account(
    gym_account_id: int,
    operator_id: int = Depends(require_session),
) -> int:
    """Resolve a path ``gym_account_id`` and assert the caller owns it.

    The shared authorization seam for every gym-account-scoped route
    (cookie refresh, deactivate/reactivate). It reads the account id
    from the request path, then confirms a row exists whose
    ``user_id`` equals the session operator. Any mismatch — a
    non-existent id or another user's account — raises a 404 with no
    data returned and no state changed (SEC-002 / CC-003: an IDOR
    probe is indistinguishable from a missing resource).

    Returns the validated ``gym_account_id`` so the route can load
    whatever columns it needs inside its own transaction.
    """
    with get_session() as session:
        owned = session.scalars(
            select(GymAccount.id).where(
                GymAccount.id == gym_account_id,
                GymAccount.user_id == operator_id,
            )
        ).first()
    if owned is None:
        raise HTTPException(status_code=404, detail="gym account not found")
    return gym_account_id


__all__ = [
    "DEFAULT_LOGIN_PATH",
    "AuthRedirectRequired",
    "require_owned_gym_account",
    "require_session",
]
