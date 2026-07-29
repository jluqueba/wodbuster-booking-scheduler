"""Active-gym selection for the web UI (approach A: global switcher).

There is no persisted default gym: the acting gym lives in the web
session and is always changeable from the nav switcher. When nothing is
selected yet, the switcher defaults to the first gym (alphabetical) so it
is never left empty; the operator can switch at any time. Telegram (a
channel with no web session) keeps using
:func:`resolve_sole_gym_account_id` instead.
"""

from __future__ import annotations

from collections.abc import MutableMapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from starlette.requests import Request

from ..auth.csrf import get_csrf_token
from ..persistence.engine import get_session
from ..persistence.models import GymAccount

# Web-session key holding the id of the gym the operator is acting on.
SESSION_KEY = "active_gym_account_id"


@dataclass(frozen=True)
class GymOption:
    """One selectable gym account (active, owned by the current user)."""

    id: int
    slug: str
    display_name: str


@dataclass(frozen=True)
class GymNav:
    """Switcher state resolved for the current request."""

    options: list[GymOption]
    active_id: int | None

    @property
    def no_gyms(self) -> bool:
        return not self.options

    @property
    def needs_selection(self) -> bool:
        return self.active_id is None and len(self.options) > 1

    @property
    def active(self) -> GymOption | None:
        return next((o for o in self.options if o.id == self.active_id), None)


def resolve_gym_nav(
    session: Session,
    user_id: int,
    web_session: MutableMapping[str, Any],
) -> GymNav:
    """Compute switcher state and persist the resolved active gym.

    Keeps a still-valid stored choice; otherwise defaults to the first
    gym (alphabetical) so the switcher is never empty. Drops a stored id
    that no longer maps to an active owned account (for example after the
    operator deactivates the gym they had selected).
    """
    rows = session.execute(
        select(GymAccount.id, GymAccount.gym_slug, GymAccount.display_name)
        .where(GymAccount.user_id == user_id, GymAccount.active.is_(True))
        .order_by(func.lower(GymAccount.display_name), GymAccount.id)
    ).all()
    options = [GymOption(id=r.id, slug=r.gym_slug, display_name=r.display_name) for r in rows]
    valid_ids = {o.id for o in options}

    stored = web_session.get(SESSION_KEY)
    active_id = stored if isinstance(stored, int) and stored in valid_ids else None
    if active_id is None and options:
        # Default to the first gym (alphabetical) so the switcher never
        # sits empty; the operator can still switch at any time.
        active_id = options[0].id

    if active_id is None:
        web_session.pop(SESSION_KEY, None)
    else:
        web_session[SESSION_KEY] = active_id

    return GymNav(options=options, active_id=active_id)


def get_gym_nav(request: Request) -> GymNav:
    """Return the request-scoped switcher state (cached on ``request.state``)."""
    cached = getattr(request.state, "gym_nav", None)
    if isinstance(cached, GymNav):
        return cached
    operator_id = request.session.get("operator_id")
    if not isinstance(operator_id, int):
        nav = GymNav(options=[], active_id=None)
    else:
        with get_session() as session:
            nav = resolve_gym_nav(session, operator_id, request.session)
    request.state.gym_nav = nav
    return nav


def active_gym_account_id(request: Request) -> int | None:
    """Return the id of the gym the current web session is acting on."""
    return get_gym_nav(request).active_id


def register_gym_globals(env: Any) -> None:
    """Attach the switcher helpers to a Jinja2 environment."""
    env.globals["gym_nav"] = get_gym_nav
    env.globals["nav_csrf"] = lambda request: get_csrf_token(request) or ""


__all__ = [
    "SESSION_KEY",
    "GymNav",
    "GymOption",
    "active_gym_account_id",
    "get_gym_nav",
    "register_gym_globals",
    "resolve_gym_nav",
]
