"""SQLite engine and session factory.

Owned by the worker process. One ``Engine`` per process (SQLAlchemy
best practice), one ``sessionmaker`` bound to it. Consumers grab a
``Session`` via the ``get_session()`` context manager, which commits on
success and rolls back on exception.

Pragmas applied on every connection via a ``connect`` event listener:

- ``foreign_keys=ON`` — SQLite requires this per connection; foreign
  key declarations are otherwise ignored.
- ``journal_mode=DELETE`` — the classic rollback journal. We would
  prefer WAL for lower write latency, but Container Apps mounts the
  Azure Files share as SMB/CIFS and SMB does not reliably support the
  POSIX shared-memory (``mmap``) semantics that WAL requires for its
  ``.db-shm`` file. The concrete failure mode is a `database is locked`
  error on the very first ``CREATE TABLE`` from Alembic. ``DELETE``
  mode uses only a plain ``.db-journal`` file (regular POSIX I/O),
  which SMB handles correctly. Trade-off: slightly higher write
  latency; acceptable for our write cadence (a handful of transactions
  per booking cycle) and reinforced by ADR-0001's ``max-replicas=1``
  single-writer guarantee.
- ``synchronous=NORMAL`` — kept from the WAL era. With ``DELETE`` mode
  SQLite promotes this internally where needed; setting it explicitly
  keeps the pragma sequence stable and self-documenting.

The engine is intentionally *not* constructed at import time. Tests
that need a private database call ``build_engine(path)`` with an
explicit override; the FastAPI app calls ``get_engine()`` once at
startup, which reads ``Settings.sqlite_path``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from ..config import get_settings

_engine: Engine | None = None
_SessionLocal: sessionmaker[Session] | None = None


def _install_pragmas(engine: Engine) -> None:
    """Attach a ``connect`` listener that enforces the SQLite pragmas.

    Registered per engine (not globally) so tests that build private
    engines pick up the same behaviour without polluting the default
    engine.
    """

    @event.listens_for(engine, "connect")
    def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA journal_mode=DELETE")
            cursor.execute("PRAGMA synchronous=NORMAL")
        finally:
            cursor.close()


def build_engine(sqlite_path: Path) -> Engine:
    """Construct a fresh engine bound to ``sqlite_path``.

    Callers own the returned engine's lifetime. Prefer ``get_engine()``
    for the process-wide singleton in application code; use this
    directly in tests that need isolation.
    """
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    url = f"sqlite:///{sqlite_path.as_posix()}"
    # ``future=True`` is the default in SQLAlchemy 2.x; passed explicitly
    # to signal intent. ``check_same_thread=False`` is safe here because
    # scoped sessions serialize connection use.
    engine = create_engine(url, future=True, connect_args={"check_same_thread": False})
    _install_pragmas(engine)
    return engine


def get_engine() -> Engine:
    """Return the process-wide engine, building it on first use.

    The engine is memoized. Tests that need to swap it out should use
    ``reset_engine()`` in a fixture teardown.
    """
    global _engine, _SessionLocal
    if _engine is None:
        settings = get_settings()
        if settings.sqlite_path is None:
            # Guarded by the ``Settings`` model validator, but re-checked
            # here to satisfy mypy and to fail loudly if the invariant
            # is ever bypassed by direct construction.
            raise RuntimeError("Settings.sqlite_path is not set")
        _engine = build_engine(settings.sqlite_path)
        _SessionLocal = sessionmaker(
            bind=_engine, autoflush=False, autocommit=False, expire_on_commit=False
        )
    return _engine


def reset_engine() -> None:
    """Dispose the process-wide engine.

    Tests call this in teardown to release file handles on Windows so
    ``tmp_path`` cleanup does not fail with ``WinError 32``.
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
