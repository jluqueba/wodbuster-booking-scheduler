"""Postgres engine and session factory.

Owned by the worker process. One ``Engine`` per process (SQLAlchemy
best practice), one ``sessionmaker`` bound to it. Consumers grab a
``Session`` via the ``get_session()`` context manager, which commits on
success and rolls back on exception.

Connection identity (ADR-0005):

- ``prod``: the connection password is a fresh Entra access token for
  the ``ossrdbms-aad`` audience, fetched via
  ``azure.identity.DefaultAzureCredential``. The token is cached
  in-process behind a lock and refreshed 5 minutes before expiry.
  Tokens are injected via SQLAlchemy's ``do_connect`` event hook so
  the pool never carries a stale credential across the refresh
  boundary.
- ``local``: the plain ``postgres_password`` from ``Settings`` is used
  (docker-compose default is ``wodbuster``). No token machinery is
  invoked.

Pool settings are the same in both modes:
``pool_size=5, max_overflow=5, pool_pre_ping=True, pool_recycle=1800``.
``pool_pre_ping`` handles Azure Database for PostgreSQL Flexible
Server's silent connection drops after a period of idleness; the
1800-second recycle keeps connections shorter than the Entra token
lifetime so a stale token never lingers past its expiry.

The engine is intentionally *not* constructed at import time. Tests
that need a private engine call ``build_engine(url_or_settings)``; the
FastAPI app calls ``get_engine()`` once at startup.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import Settings, get_settings

_POOL_KWARGS: dict[str, Any] = {
    "pool_size": 5,
    "max_overflow": 5,
    "pool_pre_ping": True,
    "pool_recycle": 1800,
}
_ENTRA_SCOPE = "https://ossrdbms-aad.database.windows.net/.default"
# Refresh a bit before the token actually expires so we never race
# against an in-flight connection acquisition. 5 minutes is well
# within the ~60-minute lifetime.
_TOKEN_REFRESH_LEEWAY_SECONDS = 5 * 60

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


@dataclass
class _CachedToken:
    """Container for the current Entra token and its absolute expiry."""

    value: str
    # Unix timestamp (seconds) at which ``azure.identity`` reported the
    # token would expire. We refresh ``_TOKEN_REFRESH_LEEWAY_SECONDS``
    # ahead of this.
    expires_at: float


class _EntraTokenProvider:
    """Thread-safe cache around ``DefaultAzureCredential``.

    One instance per engine. The FastAPI process only ever builds one
    engine, so the cache is effectively process-wide; tests that call
    ``build_engine`` repeatedly get a fresh cache each time, which is
    the isolation semantics they want.
    """

    def __init__(self) -> None:
        # ``DefaultAzureCredential`` is heavy on first import (probes
        # every credential source), so we lazy-load it. This keeps
        # local-mode tests from paying that cost.
        self._credential: Any = None
        self._cached: _CachedToken | None = None
        self._lock = threading.Lock()

    def get_password(self) -> str:
        """Return a fresh Entra access token, refreshing if needed."""
        now = time.time()
        # Fast path: read under the lock so we never hand out a token
        # another thread is about to replace.
        with self._lock:
            if (
                self._cached is not None
                and self._cached.expires_at - _TOKEN_REFRESH_LEEWAY_SECONDS > now
            ):
                return self._cached.value
            token = self._acquire_token()
            self._cached = token
            return token.value

    def _acquire_token(self) -> _CachedToken:
        if self._credential is None:
            # Imported inside the method so importing ``engine`` in
            # local mode does not pull in ``azure.identity``.
            from azure.identity import DefaultAzureCredential

            self._credential = DefaultAzureCredential()
        # ``get_token`` returns an ``AccessToken`` with ``token`` and
        # ``expires_on`` (unix timestamp) attributes.
        access = self._credential.get_token(_ENTRA_SCOPE)
        return _CachedToken(value=access.token, expires_at=float(access.expires_on))


def _install_entra_token_listener(
    engine: Engine, provider: _EntraTokenProvider
) -> None:
    """Attach a ``do_connect`` listener that injects a fresh token.

    SQLAlchemy fires ``do_connect`` immediately before the DBAPI
    ``connect`` call. Mutating ``cparams["password"]`` here means the
    token never sits in the URL and every new connection picks up the
    current cache value.
    """

    @event.listens_for(engine, "do_connect")
    def _inject_token(
        _dialect: Any,
        _conn_rec: Any,
        _cargs: tuple[Any, ...],
        cparams: dict[str, Any],
    ) -> None:
        cparams["password"] = provider.get_password()


def build_engine(url_or_settings: str | Settings) -> Engine:
    """Construct a fresh engine bound to ``url_or_settings``.

    Callers own the returned engine's lifetime. Prefer ``get_engine()``
    for the process-wide singleton in application code; use this
    directly in tests that need isolation.

    Accepts either a fully-qualified SQLAlchemy URL string (useful for
    component tests that want to point at a temp database) or a
    ``Settings`` instance from which the URL and connection identity
    are derived. When a raw URL is passed, no Entra token flow is
    attached and the URL is expected to contain any credentials it
    needs.
    """
    if isinstance(url_or_settings, Settings):
        settings = url_or_settings
        url = settings.require_postgres_dsn()
        if settings.wodbuster_env == "prod":
            provider = _EntraTokenProvider()
            engine = create_engine(url, future=True, **_POOL_KWARGS)
            _install_entra_token_listener(engine, provider)
            return engine
        # Local mode: pass the plain password through ``connect_args``.
        # The DSN itself omits it so error messages and logs stay clean.
        connect_args: dict[str, Any] = {}
        if settings.postgres_password:
            connect_args["password"] = settings.postgres_password
        return create_engine(
            url, future=True, connect_args=connect_args, **_POOL_KWARGS
        )
    # Raw URL path. Trusted for tests that construct their own DSN.
    return create_engine(url_or_settings, future=True, **_POOL_KWARGS)


def get_engine() -> Engine:
    """Return the process-wide engine, building it on first use.

    The engine is memoized. Tests that need to swap it out should use
    ``reset_engine()`` in a fixture teardown.
    """
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        _engine = build_engine(settings)
        _SessionLocal = sessionmaker(
            bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _engine


def reset_engine() -> None:
    """Dispose the process-wide engine.

    Tests call this in teardown to release pooled connections between
    fixtures.
    """
    global _engine, _SessionLocal
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _SessionLocal = None


@contextmanager
def get_session() -> Iterator[Session]:
    """Yield a session with commit-on-success, rollback-on-exception.

    Use inside route handlers and scheduler jobs. The session is closed
    unconditionally in the ``finally`` branch.
    """
    if _SessionLocal is None:
        get_engine()  # side-effect: builds _SessionLocal
    assert _SessionLocal is not None  # for mypy; get_engine() sets it
    session = _SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


__all__ = ["build_engine", "get_engine", "get_session", "reset_engine"]
