"""Shared component-test fixtures for the auth tests.

Provides:

- :func:`app_factory`: builds a fresh :class:`FastAPI` app with a
  fabricated :class:`Secrets` payload so :func:`build_session_middleware`
  gets a valid session key without touching Key Vault or ``.env``.
- :func:`postgres_engine`: a per-test schema on the local Postgres
  (docker-compose default) with the Alembic ``head`` migration
  applied. Rebinds ``persistence.engine`` module globals so the
  routes see the same schema. Skips when Postgres is unreachable.
- :func:`seed_operator` fixture: helper that inserts an
  ``operator_profile`` + ``federated_identity`` and returns their IDs.

Kept in the component directory (not top-level ``tests/conftest.py``)
so unit tests keep running without a Postgres dependency.
"""

from __future__ import annotations

import html
import os
import re
import socket
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from wodbuster_worker import persistence
from wodbuster_worker.app import create_app
from wodbuster_worker.config import Settings
from wodbuster_worker.security.keyvault import Secrets

_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def _postgres_env() -> tuple[str, int, str, str, str]:
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    db = os.environ.get("POSTGRES_DB", "wodbuster")
    user = os.environ.get("POSTGRES_USER", "wodbuster")
    password = os.environ.get("POSTGRES_PASSWORD", "wodbuster")
    return host, port, db, user, password


def _postgres_reachable(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


@pytest.fixture
def postgres_engine(monkeypatch: pytest.MonkeyPatch) -> Iterator[Engine]:
    """Yield a migrated per-test-schema engine and rebind persistence globals.

    The auth routes reach into :mod:`persistence.engine` via
    :func:`db_session`, which builds the process-wide engine on first
    call and caches it. Rebinding the module globals in place is
    simpler than refactoring the routes for injection and mirrors the
    approach the F4.T2 migration test takes.
    """
    host, port, db, user, password = _postgres_env()
    if not _postgres_reachable(host, port):
        pytest.skip(f"Postgres not reachable at {host}:{port}")

    schema = f"test_{uuid.uuid4().hex[:12]}"
    url = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"

    admin = create_engine(url, future=True)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    scoped_engine = create_engine(
        url,
        future=True,
        connect_args={"options": f"-csearch_path={schema}"},
    )

    cfg = Config(str(_ALEMBIC_INI))
    with scoped_engine.begin() as conn:
        cfg.attributes["connection"] = conn
        cfg.attributes["version_table_schema"] = schema
        command.upgrade(cfg, "head")

    # Rebind the process-wide engine + session factory used by the
    # routes. Store originals so we can restore on teardown.
    engine_module = persistence.engine
    session_local = sessionmaker(
        bind=scoped_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )
    monkeypatch.setattr(engine_module, "_engine", scoped_engine, raising=False)
    monkeypatch.setattr(engine_module, "_SessionLocal", session_local, raising=False)

    try:
        yield scoped_engine
    finally:
        scoped_engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def app_factory(
    postgres_engine: Engine,
) -> Callable[..., FastAPI]:
    """Return a factory that builds a fresh test app.

    Injects a fabricated :class:`Settings` (with dummy OAuth client
    IDs so :func:`build_oauth` registers the three providers) and
    :class:`Secrets` (with a valid session encryption secret). Tests
    that want to short-circuit OAuth patch ``app.state.oauth`` after
    construction.
    """

    def _build(**overrides: Any) -> FastAPI:
        settings = Settings(
            # Hermetic: never read the developer's repo-root ``.env``.
            # Otherwise locally-set values (e.g. WODBUSTER_GYM/IDU) leak
            # in and flip stack-wiring assertions.
            _env_file=None,  # type: ignore[call-arg]
            wodbuster_env="local",
            postgres_host="localhost",
            postgres_db="wodbuster",
            postgres_user="wodbuster",
            postgres_password="wodbuster",
            oauth_microsoft_client_id="test-ms-client",
            oauth_github_client_id="test-gh-client",
            oauth_google_client_id="test-google-client",
            session_idle_minutes=30,
            session_absolute_hours=24,
            **overrides,
        )
        secrets = Secrets(
            session_encryption_secret="a" * 32,
            oauth_microsoft_client_secret="test-ms-secret",
            oauth_github_client_secret="test-gh-secret",
            oauth_google_client_secret="test-google-secret",
        )
        return create_app(settings=settings, secrets=secrets)

    return _build


@pytest.fixture
def seed_operator(postgres_engine: Engine) -> Callable[..., tuple[int, str]]:
    """Return a helper that inserts an operator + federated identity + gym account.

    Returns ``(operator_id, subject_id)`` so tests can drive the
    callback path with a subject they know is on the allow-list.

    Multi-gym (ADR-0007): also seeds ONE ``gym_account`` for the
    operator so route-level tests resolve a sole gym account through
    :func:`resolve_sole_gym_account_id` without extra wiring. Tests that
    directly insert booking-scoped rows resolve the id via
    :func:`gym_account_id_for`.
    """

    def _insert(
        *,
        provider: str = "microsoft",
        subject_id: str | None = None,
        display_name: str = "Test Operator",
    ) -> tuple[int, str]:
        actual_subject = subject_id or f"sub-{uuid.uuid4().hex[:12]}"
        with postgres_engine.begin() as conn:
            op_id = conn.execute(
                text("INSERT INTO operator_profile (display_name) VALUES (:n) RETURNING id"),
                {"n": display_name},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO federated_identity "
                    "(operator_id, provider, subject_id, display_name) "
                    "VALUES (:op, :p, :s, :n)"
                ),
                {"op": op_id, "p": provider, "s": actual_subject, "n": display_name},
            )
            conn.execute(
                text(
                    "INSERT INTO gym_account (user_id, gym_slug, display_name, idu) "
                    "VALUES (:op, 'antworktrainingcenter', :n, :idu)"
                ),
                {"op": op_id, "n": display_name, "idu": f"idu{op_id:032d}"[:32]},
            )
        return int(op_id), actual_subject

    return _insert


def gym_account_id_for(conn: Any, operator_id: int) -> int:
    """Return (creating if needed) the sole gym-account id for ``operator_id``.

    Test helper for the multi-gym bridge (ADR-0007): booking-scoped rows
    key off ``gym_account_id``, but tests seed and reason in terms of the
    operator. ``seed_operator`` already creates one gym account per
    operator; this is get-or-create so component helpers that build an
    operator via their own local fixtures (not ``seed_operator``) also
    resolve a gym account without extra wiring. ``conn`` is any object
    exposing ``execute`` (a SQLAlchemy ``Connection`` or ``Session``).
    """
    existing = conn.execute(
        text("SELECT id FROM gym_account WHERE user_id = :op ORDER BY id LIMIT 1"),
        {"op": operator_id},
    ).scalar()
    if existing is not None:
        return int(existing)
    return int(
        conn.execute(
            text(
                "INSERT INTO gym_account (user_id, gym_slug, display_name, idu) "
                "VALUES (:op, 'antworktrainingcenter', 'Test Gym', :idu) RETURNING id"
            ),
            {"op": operator_id, "idu": f"idu{operator_id:032d}"[:32]},
        ).scalar_one()
    )


# ---------------------------------------------------------------------------
# Rendered-markup helpers
# ---------------------------------------------------------------------------

# A form asks for confirmation by carrying the prompt as text in a data
# attribute; a delegated listener reads it back with getAttribute, which
# is what html.unescape models here.
_CONFIRM_ATTRIBUTE = re.compile(
    r"""data-wb-confirm=(?P<quote>["'])(?P<message>(?!(?P=quote)).*?)(?P=quote)""",
    re.DOTALL,
)


def confirm_messages(body: str) -> list[str]:
    """Return the confirmation prompt of every guarded form in ``body``.

    The prompt lives in ``data-wb-confirm`` as plain text, so the only
    transformation between the markup and what the operator reads is the
    HTML entity decoding every parser applies. Asserting on the decoded
    value keeps the guard honest about apostrophes and quotes without
    reproducing any JavaScript parsing.
    """
    return [
        html.unescape(attribute.group("message")) for attribute in _CONFIRM_ATTRIBUTE.finditer(body)
    ]
