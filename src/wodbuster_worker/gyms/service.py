"""Gym-account service: discover accessible gyms and store the shared cookie.

A single ``.WBAuth`` session authenticates every WodBuster gym the identity
can access, so gyms are never added by hand. Discovery reads the authenticated
central selector (ADR-0009) and, for each gym not already owned, validates the
cookie and reads the operator's own idu (SEC-003) before writing the
``gym_account`` row and its AES-256-GCM-bound cookie in one transaction. A
cookie paste is applied to every owned gym at once.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..heartbeat.alerts import close_open_cookie_expiring
from ..persistence.cookie_store import CookieStore
from ..persistence.models import GymAccount
from ..wodbuster_client.client import WodBusterClient, WodBusterClientFactory
from .discovery import DiscoveredGym, is_valid_discovered_slug


def gym_client_factory(app_state: Any) -> WodBusterClientFactory | None:
    """Return the per-gym client factory usable by the booking paths.

    Prefers ``app_state.booking_client_factory`` (the real per-gym factory
    wired in prod). Falls back to wrapping a single
    ``app_state.wodbuster_client`` so single-gym deployments and tests that
    inject one client keep working without a factory. Returns ``None`` when
    neither is wired.
    """
    factory = getattr(app_state, "booking_client_factory", None)
    if isinstance(factory, WodBusterClientFactory):
        return factory
    client = getattr(app_state, "wodbuster_client", None)
    if client is not None:
        # Duck-typed clients (test fakes) are accepted here; the real
        # per-gym factory is preferred above when wired.
        return WodBusterClientFactory(builder=lambda gym, idu: client)
    return None


def resolve_gym_client(
    factory: WodBusterClientFactory,
    session: Session,
    gym_account_id: int,
) -> tuple[WodBusterClient, str] | None:
    """Return the ``(client, idu)`` for a gym account, or ``None``.

    The per-gym-account client is memoised on ``factory`` by
    ``(gym_slug, idu)``, so booking, manual booking, cancellation, the
    rule picker, and vacation bulk-cancel all target the gym account's
    own subdomain + ``idu`` (ADR-0007, P2b) rather than a single global
    gym. Returns ``None`` when the gym account no longer exists.
    """
    gym_account = session.get(GymAccount, gym_account_id)
    if gym_account is None:
        return None
    return factory.get(gym_account), gym_account.idu


class DiscoveryClientProtocol(Protocol):
    """Minimal interface the discovery flow needs from a WodBuster client."""

    def discover_idu(self, cookie_value: str) -> str:  # pragma: no cover - protocol
        ...


class GymAlreadyExistsError(Exception):
    """Raised when the user already owns a gym account for this slug."""


def _persist_gym_account(
    session: Session,
    *,
    user_id: int,
    gym_slug: str,
    cookie_value: str,
    cookie_store: CookieStore,
    client: DiscoveryClientProtocol,
    display_name: str | None,
    now: datetime | None,
) -> int:
    existing = session.execute(
        select(GymAccount.id).where(
            GymAccount.user_id == user_id,
            GymAccount.gym_slug == gym_slug,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise GymAlreadyExistsError(f"gym account already exists for {gym_slug!r}")

    idu = client.discover_idu(cookie_value)
    label = (display_name or gym_slug).strip() or gym_slug
    account = GymAccount(
        user_id=user_id,
        gym_slug=gym_slug,
        display_name=label,
        idu=idu,
        active=True,
    )
    session.add(account)
    session.flush()
    cookie_store.save(
        session,
        int(account.id),
        cookie_value,
        validated_at=now or datetime.now(tz=UTC),
    )
    return int(account.id)


def add_discovered_gym_accounts(
    session: Session,
    *,
    user_id: int,
    gyms: list[DiscoveredGym],
    cookie_value: str,
    cookie_store: CookieStore,
    client_factory: Callable[[str], DiscoveryClientProtocol],
    now: datetime | None = None,
) -> list[str]:
    """Validate and persist selector-attested gyms not already owned."""
    owned = set(
        session.scalars(select(GymAccount.gym_slug).where(GymAccount.user_id == user_id)).all()
    )
    added: list[str] = []
    for gym in gyms:
        if gym.slug in owned:
            continue
        if not is_valid_discovered_slug(gym.slug):
            raise ValueError(f"invalid selector-derived gym slug {gym.slug!r}")
        _persist_gym_account(
            session,
            user_id=user_id,
            gym_slug=gym.slug,
            cookie_value=cookie_value,
            cookie_store=cookie_store,
            client=client_factory(gym.slug),
            display_name=gym.display_name,
            now=now,
        )
        owned.add(gym.slug)
        added.append(gym.slug)
    return added


def store_cookie_for_all_gyms(
    session: Session,
    *,
    user_id: int,
    cookie_value: str,
    cookie_store: CookieStore,
    now: datetime | None = None,
) -> int:
    """Save one validated cookie to every gym account the user owns.

    A single ``.WBAuth`` session authenticates every gym the identity can
    access, so a paste applies to all of them. Each gym keeps its own
    encrypted row (the ciphertext is bound to its ``gym_account_id``), and any
    open ``cookie_expiring`` alert is closed so banners clear immediately.
    Returns the number of gyms updated. The caller owns the transaction.
    """
    when = now or datetime.now(tz=UTC)
    account_ids = session.scalars(select(GymAccount.id).where(GymAccount.user_id == user_id)).all()
    for gym_account_id in account_ids:
        cookie_store.save(session, int(gym_account_id), cookie_value, validated_at=when)
        close_open_cookie_expiring(session, int(gym_account_id), now=when)
    return len(account_ids)


__all__ = [
    "DiscoveryClientProtocol",
    "GymAlreadyExistsError",
    "add_discovered_gym_accounts",
    "gym_client_factory",
    "resolve_gym_client",
    "store_cookie_for_all_gyms",
]
