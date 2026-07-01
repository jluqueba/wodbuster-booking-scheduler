"""Alembic environment.

Reads the target database URL from the worker's ``Settings`` (which in
turn resolves ``SQLITE_PATH`` via env or ``.env``), so the same
migration script works locally, in tests, and in the Container Apps
runtime without hand-editing ``alembic.ini``.

Two override hooks are supported for tests and container startup:

- ``sqlalchemy.url`` in ``alembic.ini`` — if set, wins over the
  Settings-derived URL. Useful for one-off migrations against an
  arbitrary path.
- ``config.attributes["connection"]`` — if a SQLAlchemy ``Connection``
  is passed programmatically (via ``command.upgrade(config, "head")``
  after ``config.attributes["connection"] = conn``), Alembic runs
  against it directly. Used by the component test to migrate a temp
  database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from wodbuster_worker.config import get_settings
from wodbuster_worker.persistence import Base
from wodbuster_worker.persistence import (
    models as _models,  # noqa: F401  (side-effect: register mappers)
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_url() -> str:
    """Prefer an explicit ini URL; otherwise derive from ``Settings``."""
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    settings = get_settings()
    if settings.sqlite_path is None:
        raise RuntimeError("Settings.sqlite_path is not set; cannot run migrations.")
    return f"sqlite:///{settings.sqlite_path.as_posix()}"


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    # Support programmatic invocation with a pre-built connection so
    # component tests can point at a temp database without mutating the
    # ini file.
    injected = config.attributes.get("connection", None)
    if injected is not None:
        context.configure(
            connection=injected,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    ini_section = config.get_section(config.config_ini_section, {}) or {}
    ini_section["sqlalchemy.url"] = _resolve_url()
    connectable = engine_from_config(
        ini_section,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
