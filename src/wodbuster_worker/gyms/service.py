"""Add-gym service: validate a gym, discover its idu, persist atomically.

The add-gym flow (FR-005, FR-011, FR-012) lets a user register a second
WodBuster gym. Given a candidate gym slug and a freshly pasted ``.WBAuth``
cookie, it:

1. enforces the curated allow-list server-side (SEC-001): the slug must be
   an exact member of ``Settings.known_gym_slugs()`` and match the DNS-label
   syntax before any URL is built from it;
2. uses a discovery-only client already built for that gym to call
   ``discover_idu``, which validates the cookie AND reads the operator's own
   idu from the authenticated page (SEC-003);
3. writes the ``gym_account`` row and the AES-256-GCM-bound cookie in one
   transaction. Nothing is persisted unless both validation and discovery
   succeed, so there is never a half-provisioned account (FR-011).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..persistence.cookie_store import CookieStore
from ..persistence.models import GymAccount


class DiscoveryClientProtocol(Protocol):
    """Minimal interface the add-gym flow needs from a WodBuster client."""

    def discover_idu(self, cookie_value: str) -> str:  # pragma: no cover - protocol
        ...


class GymAlreadyExistsError(Exception):
    """Raised when the user already owns a gym account for this slug."""


def add_gym_account(
    session: Session,
    *,
    user_id: int,
    gym_slug: str,
    cookie_value: str,
    settings: Settings,
    cookie_store: CookieStore,
    client: DiscoveryClientProtocol,
    display_name: str | None = None,
    now: datetime | None = None,
) -> int:
    """Validate and create a gym account atomically; return its id.

    ``client`` must be a discovery-only client already constructed for
    ``gym_slug`` (the caller owns client construction so this stays
    testable and so the base URL is built only from the validated slug).

    Raises:
      - ``ValueError``: slug not on the allow-list (SEC-001) or blank cookie.
      - :class:`GymAlreadyExistsError`: the user already has this gym.
      - ``WodBusterAuthError`` / ``WodBusterProtocolError`` /
        ``WodBusterTransportError``: the cookie was rejected or the idu
        could not be discovered.

    The caller owns the transaction (commit / rollback).
    """
    slug = settings.validate_gym_slug(gym_slug)  # SEC-001: exact allow-list + regex
    if not cookie_value.strip():
        raise ValueError("cookie value must not be blank")

    existing = session.execute(
        select(GymAccount.id).where(
            GymAccount.user_id == user_id,
            GymAccount.gym_slug == slug,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise GymAlreadyExistsError(f"gym account already exists for {slug!r}")

    # Validates the cookie AND discovers the operator's own idu (SEC-003).
    # Any failure raises before a row is written (FR-011: atomic).
    idu = client.discover_idu(cookie_value)

    label = (display_name or slug).strip() or slug
    account = GymAccount(
        user_id=user_id,
        gym_slug=slug,
        display_name=label,
        idu=idu,
        active=True,
    )
    session.add(account)
    session.flush()  # populate account.id and make it queryable for the store

    cookie_store.save(
        session,
        int(account.id),
        cookie_value,
        validated_at=now or datetime.now(tz=UTC),
    )
    return int(account.id)


__all__ = ["DiscoveryClientProtocol", "GymAlreadyExistsError", "add_gym_account"]
