"""Alembic environment.

Reads the target Postgres URL from the worker's ``Settings`` (which in
turn reads ``POSTGRES_*`` env vars via ``.env`` or the process
environment), so the same migration script works locally against
docker-compose, in the component test suite against a temp schema, and
in the Container Apps runtime against Azure Database for PostgreSQL
Flexible Server without hand-editing ``alembic.ini``.

In ``prod`` mode the runtime UAMI's Entra token is injected on every
connect via the same ``do_connect`` machinery ``persistence.engine``
uses (see ADR-0005). In ``local`` mode the plain
``POSTGRES_PASSWORD`` from ``.env`` is used.

Two override hooks are supported:

- ``sqlalchemy.url`` in ``alembic.ini`` — if set, wins over the
  Settings-derived URL. Useful for one-off migrations against an
  arbitrary DSN.
- ``config.attributes["connection"]`` — if a SQLAlchemy ``Connection``
  is passed programmatically (via ``command.upgrade(config, "head")``
  after ``config.attributes["connection"] = conn``), Alembic runs
  against it directly. Used by component tests to migrate a temp
  database.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from wodbuster_worker.config import Settings, get_settings
from wodbuster_worker.persistence import Base
from wodbuster_worker.persistence import (
    models as _models,  # noqa: F401  (side-effect: register mappers)
)
from wodbuster_worker.persistence.engine import (
    _EntraTokenProvider,
    _install_entra_token_listener,
)

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_settings() -> Settings:
    """Return the cached ``Settings`` instance for URL/identity."""
    return get_settings()


def _resolve_url() -> str:
    """Prefer an explicit ini URL; otherwise derive from ``Settings``."""
    ini_url = config.get_main_option("sqlalchemy.url")
    if ini_url:
        return ini_url
    return _resolve_settings().require_postgres_dsn()


def run_migrations_offline() -> None:
    context.configure(
        url=_resolve_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
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
        )
        with context.begin_transaction():
            context.run_migrations()
        return

    ini_section = config.get_section(config.config_ini_section, {}) or {}
    ini_url = config.get_main_option("sqlalchemy.url")
    settings = _resolve_settings()

    if ini_url:
        # Explicit ini override: honour it verbatim, no token injection.
        ini_section["sqlalchemy.url"] = ini_url
        connectable = engine_from_config(
            ini_section,
            prefix="sqlalchemy.",
            poolclass=pool.NullPool,
        )
    else:
        # Settings-driven URL. NullPool for one-shot migrations, plus
        # the token listener in prod so the connection presents an
        # Entra token instead of a password.
        ini_section["sqlalchemy.url"] = settings.require_postgres_dsn()
        if settings.wodbuster_env == "prod":
            connectable = engine_from_config(
                ini_section,
                prefix="sqlalchemy.",
                poolclass=pool.NullPool,
            )
            _install_entra_token_listener(connectable, _EntraTokenProvider())
        else:
            connect_args: dict[str, object] = {}
            if settings.postgres_password:
                connect_args["password"] = settings.postgres_password
            connectable = engine_from_config(
                ini_section,
                prefix="sqlalchemy.",
                poolclass=pool.NullPool,
                connect_args=connect_args,
            )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
