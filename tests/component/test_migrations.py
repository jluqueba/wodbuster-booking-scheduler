"""Foundational tests for the Alembic baseline migration (F4.T2).

Runs ``alembic upgrade head`` programmatically against a real Postgres
instance (docker-compose locally, service container in CI), then:

- asserts every one of the ten declared tables exists in the target
  schema;
- inserts a minimal row into each and reads it back, exercising the
  concrete column types (LargeBinary/BYTEA, DateTime/TIMESTAMPTZ,
  native Enum) end-to-end.

These are the load-bearing checks that keep the baseline migration
honest against ``persistence.models``. Autogenerate diffs and schema
drift show up here first.

Isolation model: each test gets a per-test Postgres schema whose name
is derived from ``tmp_path``. Alembic runs against that schema via
``version_table_schema`` + ``include_schemas`` context configuration.
Tests can therefore run in parallel without stepping on each other
even against a single shared Postgres.

Tests skip if the local Postgres coordinates are not reachable (e.g.
`docker compose up postgres` has not been run and CI is not configured
with a service container).
"""

from __future__ import annotations

import os
import socket
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "alert",
        "booking_outcome",
        "class_preference",
        "cookie_credential",
        "federated_identity",
        "heartbeat_reading",
        "notification_outbox",
        "operator_profile",
        "scheduler_rule",
        "vacation_window",
    }
)

# Resolve alembic.ini relative to the repo root so pytest can invoke
# this test from any working directory (VS Code, CI, ad-hoc).
_REPO_ROOT = Path(__file__).resolve().parents[2]
_ALEMBIC_INI = _REPO_ROOT / "alembic.ini"


def _postgres_env() -> tuple[str, int, str, str, str]:
    """Return (host, port, db, user, password) for the test Postgres.

    Reads from POSTGRES_* env vars with docker-compose defaults so a
    developer with ``docker compose up postgres`` gets tests for free.
    CI sets the same vars via the workflow's `env` block.
    """
    host = os.environ.get("POSTGRES_HOST", "localhost")
    port = int(os.environ.get("POSTGRES_PORT", "5432"))
    db = os.environ.get("POSTGRES_DB", "wodbuster")
    user = os.environ.get("POSTGRES_USER", "wodbuster")
    password = os.environ.get("POSTGRES_PASSWORD", "wodbuster")
    return host, port, db, user, password


def _postgres_reachable(host: str, port: int) -> bool:
    """TCP-connect probe. Short timeout so we skip fast when there is no server."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        try:
            sock.connect((host, port))
        except OSError:
            return False
    return True


@pytest.fixture
def migrated_engine() -> Iterator[Engine]:
    """Yield an engine bound to a freshly migrated per-test Postgres schema.

    Rationale for per-test schema (not per-test database): CREATE
    DATABASE and DROP DATABASE are expensive on Postgres and require
    disconnecting active sessions. A per-test schema is cheap, isolates
    DDL, and lets us reuse the docker-compose ``wodbuster`` database.
    """
    host, port, db, user, password = _postgres_env()
    if not _postgres_reachable(host, port):
        pytest.skip(
            f"Postgres not reachable at {host}:{port}; run "
            "`docker compose up -d postgres` or set POSTGRES_HOST."
        )

    schema = f"test_{uuid.uuid4().hex[:12]}"
    url = f"postgresql+psycopg://{user}:{password}@{host}:{port}/{db}"

    admin = create_engine(url, future=True)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{schema}"'))

    # Build a search-path-scoped engine and hand its connection to
    # alembic via ``config.attributes["connection"]``. This avoids the
    # configparser-percent-interpolation trap that hits us if we try
    # to shove ``options=-c search_path=...`` into the URL.
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

    try:
        yield scoped_engine
    finally:
        scoped_engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{schema}" CASCADE'))
        admin.dispose()


def test_upgrade_creates_every_domain_table(migrated_engine: Engine) -> None:
    # The migration DDL runs with search_path pinned to our schema, so
    # inspect() returns tables from there. Filter out the alembic
    # bookkeeping row.
    insp = inspect(migrated_engine)
    with migrated_engine.connect() as conn:
        schema = conn.execute(text("SHOW search_path")).scalar_one()
        # search_path is a comma-separated list; the first entry is ours.
        schema = schema.split(",")[0].strip().strip('"')
    actual = set(insp.get_table_names(schema=schema))
    actual.discard("alembic_version")

    assert actual == EXPECTED_TABLES


def test_alert_partial_unique_index_present(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    with migrated_engine.connect() as conn:
        schema = conn.execute(text("SHOW search_path")).scalar_one()
        schema = schema.split(",")[0].strip().strip('"')
    names = {ix["name"] for ix in insp.get_indexes("alert", schema=schema)}

    assert "uq_alert_open_operator_kind" in names


def test_minimal_rows_round_trip_through_every_table(
    migrated_engine: Engine,
) -> None:
    """Insert one row per table and read it back.

    Uses raw SQL rather than ORM classes so this test remains a
    schema-only contract check: it exercises the exact column names
    and types written by the migration, independent of how the ORM
    models happen to look today.
    """
    now = datetime.now(UTC)

    with migrated_engine.begin() as conn:
        op_id = conn.execute(
            text(
                "INSERT INTO operator_profile (display_name) "
                "VALUES (:name) RETURNING id"
            ),
            {"name": "Alice"},
        ).scalar_one()

        rule_id = conn.execute(
            text(
                "INSERT INTO scheduler_rule "
                "(operator_id, day_of_week, window_offset_hours, active) "
                "VALUES (:op, 1, 48, TRUE) RETURNING id"
            ),
            {"op": op_id},
        ).scalar_one()

        alert_id = conn.execute(
            text(
                "INSERT INTO alert (operator_id, kind) "
                "VALUES (:op, 'cookie_expiring') RETURNING id"
            ),
            {"op": op_id},
        ).scalar_one()

        conn.execute(
            text(
                "INSERT INTO federated_identity "
                "(operator_id, provider, subject_id) "
                "VALUES (:op, 'github', 'sub-1')"
            ),
            {"op": op_id},
        )
        conn.execute(
            text(
                "INSERT INTO cookie_credential "
                "(operator_id, cookie_ciphertext, cookie_nonce) "
                "VALUES (:op, :ct, :n)"
            ),
            {"op": op_id, "ct": b"\x00\x01", "n": b"\x02\x03\x04"},
        )
        conn.execute(
            text(
                "INSERT INTO class_preference "
                "(rule_id, order_index, class_type, target_time_slot) "
                "VALUES (:r, 0, 'WOD', '18:30')"
            ),
            {"r": rule_id},
        )
        conn.execute(
            text(
                "INSERT INTO booking_outcome "
                "(operator_id, rule_id, target_class, target_slot, "
                " terminal_status) "
                "VALUES (:op, :r, 'WOD', :slot, 'granted')"
            ),
            {"op": op_id, "r": rule_id, "slot": now},
        )
        conn.execute(
            text(
                "INSERT INTO vacation_window "
                "(operator_id, start_date, end_date) "
                "VALUES (:op, :s, :e)"
            ),
            {"op": op_id, "s": now.date(), "e": now.date()},
        )
        conn.execute(
            text(
                "INSERT INTO heartbeat_reading "
                "(operator_id, result, alert_id) "
                "VALUES (:op, 'valid', :a)"
            ),
            {"op": op_id, "a": alert_id},
        )
        conn.execute(
            text(
                "INSERT INTO notification_outbox "
                "(operator_id, kind, target, payload) "
                "VALUES (:op, 'telegram', 'chat-1', '{}'::jsonb)"
            ),
            {"op": op_id},
        )

    with migrated_engine.connect() as conn:
        for table in EXPECTED_TABLES:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            assert count >= 1, f"expected at least one row in {table}, got {count}"

        stored_ct = bytes(
            conn.execute(
                text("SELECT cookie_ciphertext FROM cookie_credential")
            ).scalar_one()
        )
        assert stored_ct == b"\x00\x01"
