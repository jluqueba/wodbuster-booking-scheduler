"""Gym-account resolution helpers (ADR-0007 bridge).

The multi-gym add-account UI (US-multi-gym P1) is not yet wired. Until a
user can own several accounts, every user holds at most one
``gym_account`` (seeded by the ADR-0007 migration). Booking-scoped
service calls therefore resolve "the acting user's sole gym account"
through :func:`resolve_sole_gym_account_id` at the route/wiring
boundary, then thread the resulting ``gym_account_id`` downwards.

When a second account per user lands, callers switch to passing an
explicit account id and this helper is retired. Keeping the seam in one
place makes that migration a search-and-replace rather than a sweep.
"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import GymAccount

__all__ = [
    "list_active_gym_account_ids",
    "list_user_gym_accounts",
    "resolve_sole_gym_account_id",
]


def resolve_sole_gym_account_id(session: Session, user_id: int) -> int | None:
    """Return the user's single active gym-account id, or ``None``.

    Orders by ``id`` for determinism. Returns ``None`` when the user has
    no gym account yet (a fresh operator before the add-gym flow, or a
    fresh database). Callers treat ``None`` as "no gym configured" and
    render the empty/onboarding state rather than raising.
    """
    return session.scalars(
        select(GymAccount.id)
        .where(GymAccount.user_id == user_id, GymAccount.active.is_(True))
        .order_by(GymAccount.id)
        .limit(1)
    ).first()


def list_active_gym_account_ids(session: Session) -> list[int]:
    """Return every active gym-account id (background-job fan-out).

    The heartbeat tick and other cross-tenant sweeps iterate gym
    accounts rather than users so each account's cookie is probed
    independently.
    """
    return list(session.scalars(select(GymAccount.id).where(GymAccount.active.is_(True))).all())


def list_user_gym_accounts(session: Session, user_id: int) -> list[GymAccount]:
    """Return the user's active gym accounts, ordered by ``id``.

    Telegram has no web session and therefore no nav switcher to pick an
    active gym, so its read commands aggregate across every gym the user
    owns rather than silently acting on the first one.
    """
    return list(
        session.scalars(
            select(GymAccount)
            .where(GymAccount.user_id == user_id, GymAccount.active.is_(True))
            .order_by(GymAccount.id)
        ).all()
    )
