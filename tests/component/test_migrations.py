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
from typing import Any

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import DataError, IntegrityError

EXPECTED_TABLES: frozenset[str] = frozenset(
    {
        "alert",
        "booking_day_override",
        "booking_outcome",
        "cookie_credential",
        "federated_identity",
        "gym_account",
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

    assert "uq_alert_open_gym_account_kind" in names


def test_operator_profile_has_profile_columns(migrated_engine: Engine) -> None:
    """User Profile columns exist with the right defaults and enum guard."""
    with migrated_engine.begin() as conn:
        op_id = conn.execute(
            text("INSERT INTO operator_profile (display_name) VALUES (:n) RETURNING id"),
            {"n": "Profiled"},
        ).scalar_one()
        # communication_language defaults to 'en'; the new columns are null.
        row = conn.execute(
            text(
                "SELECT short_name, profile_picture_ref, communication_language "
                "FROM operator_profile WHERE id = :id"
            ),
            {"id": op_id},
        ).one()
    assert row.short_name is None
    assert row.profile_picture_ref is None
    assert row.communication_language == "en"

    # 'es' is accepted by the enum.
    with migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE operator_profile SET communication_language = 'es' WHERE id = :id"),
            {"id": op_id},
        )

    # A value outside the enum is rejected by the type.
    with pytest.raises(DataError), migrated_engine.begin() as conn:
        conn.execute(
            text("UPDATE operator_profile SET communication_language = 'fr' WHERE id = :id"),
            {"id": op_id},
        )


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
            text("INSERT INTO operator_profile (display_name) VALUES (:name) RETURNING id"),
            {"name": "Alice"},
        ).scalar_one()

        gym_account_id = conn.execute(
            text(
                "INSERT INTO gym_account (user_id, gym_slug, display_name, idu) "
                "VALUES (:op, 'antworktrainingcenter', 'Adwork', 'idu-1') RETURNING id"
            ),
            {"op": op_id},
        ).scalar_one()

        rule_id = conn.execute(
            text(
                "INSERT INTO scheduler_rule "
                "(gym_account_id, day_of_week, class_type, class_time, "
                "booking_opens_days_before, booking_opens_at, active) "
                "VALUES (:ga, 1, 'WOD', '18:30', 2, '21:30', TRUE) "
                "RETURNING id"
            ),
            {"ga": gym_account_id},
        ).scalar_one()

        alert_id = conn.execute(
            text(
                "INSERT INTO alert (gym_account_id, kind) "
                "VALUES (:ga, 'cookie_expiring') RETURNING id"
            ),
            {"ga": gym_account_id},
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
                "(gym_account_id, cookie_ciphertext, cookie_nonce) "
                "VALUES (:ga, :ct, :n)"
            ),
            {"ga": gym_account_id, "ct": b"\x00\x01", "n": b"\x02\x03\x04"},
        )
        conn.execute(
            text(
                "INSERT INTO booking_outcome "
                "(gym_account_id, rule_id, target_class, target_slot, "
                " terminal_status) "
                "VALUES (:ga, :r, 'WOD', :slot, 'granted')"
            ),
            {"ga": gym_account_id, "r": rule_id, "slot": now},
        )
        conn.execute(
            text(
                "INSERT INTO vacation_window "
                "(gym_account_id, start_date, end_date) "
                "VALUES (:ga, :s, :e)"
            ),
            {"ga": gym_account_id, "s": now.date(), "e": now.date()},
        )
        conn.execute(
            text(
                "INSERT INTO heartbeat_reading "
                "(gym_account_id, result, alert_id) "
                "VALUES (:ga, 'valid', :a)"
            ),
            {"ga": gym_account_id, "a": alert_id},
        )
        conn.execute(
            text(
                "INSERT INTO notification_outbox "
                "(user_id, kind, target, payload) "
                "VALUES (:op, 'telegram', 'chat-1', '{}'::jsonb)"
            ),
            {"op": op_id},
        )
        conn.execute(
            text(
                "INSERT INTO booking_day_override "
                "(rule_id, gym_account_id, target_date, class_time) "
                "VALUES (:r, :ga, :d, '07:00')"
            ),
            {"r": rule_id, "ga": gym_account_id, "d": now.date()},
        )

    with migrated_engine.connect() as conn:
        for table in EXPECTED_TABLES:
            count = conn.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar_one()
            assert count >= 1, f"expected at least one row in {table}, got {count}"

        stored_ct = bytes(
            conn.execute(text("SELECT cookie_ciphertext FROM cookie_credential")).scalar_one()
        )
        assert stored_ct == b"\x00\x01"


# ---------------------------------------------------------------------------
# a7c3d9e1f4b2 — single-day booking override (ADR-0012)
# ---------------------------------------------------------------------------


def _search_path_schema(engine: Engine) -> str:
    with engine.connect() as conn:
        schema = conn.execute(text("SHOW search_path")).scalar_one()
    return str(schema).split(",")[0].strip().strip('"')


def _seed_rule(conn: Any) -> tuple[int, int]:
    """Insert the minimum chain and return ``(gym_account_id, rule_id)``."""
    op_id = conn.execute(
        text("INSERT INTO operator_profile (display_name) VALUES ('Overrider') RETURNING id")
    ).scalar_one()
    gym_account_id = conn.execute(
        text(
            "INSERT INTO gym_account (user_id, gym_slug, display_name, idu) "
            "VALUES (:op, 'antworktrainingcenter', 'Adwork', 'idu-1') RETURNING id"
        ),
        {"op": op_id},
    ).scalar_one()
    rule_id = conn.execute(
        text(
            "INSERT INTO scheduler_rule "
            "(gym_account_id, day_of_week, class_type, class_time, "
            "booking_opens_days_before, booking_opens_at, active) "
            "VALUES (:ga, 2, 'WOD', '18:30', 2, '21:30', TRUE) RETURNING id"
        ),
        {"ga": gym_account_id},
    ).scalar_one()
    return int(gym_account_id), int(rule_id)


def test_override_revision_downgrades_and_upgrades_again(migrated_engine: Engine) -> None:
    """The revision is reversible from the current head and back."""
    schema = _search_path_schema(migrated_engine)
    cfg = Config(str(_ALEMBIC_INI))

    with migrated_engine.begin() as conn:
        cfg.attributes["connection"] = conn
        cfg.attributes["version_table_schema"] = schema
        command.downgrade(cfg, "-1")

    insp = inspect(migrated_engine)
    assert "booking_day_override" not in insp.get_table_names(schema=schema)
    outcome_columns = {c["name"] for c in insp.get_columns("booking_outcome", schema=schema)}
    assert "outcome_source" not in outcome_columns

    with migrated_engine.begin() as conn:
        cfg.attributes["connection"] = conn
        command.upgrade(cfg, "head")

    insp = inspect(migrated_engine)
    assert "booking_day_override" in insp.get_table_names(schema=schema)


def test_override_indexes_are_present(migrated_engine: Engine) -> None:
    schema = _search_path_schema(migrated_engine)
    insp = inspect(migrated_engine)

    names = {ix["name"] for ix in insp.get_indexes("booking_day_override", schema=schema)}

    assert "ix_booking_day_override_gym_date" in names


def test_outcome_source_defaults_to_rule_for_rows_without_one(migrated_engine: Engine) -> None:
    """Existing rows are covered by the server default, no backfill needed."""
    with migrated_engine.begin() as conn:
        gym_account_id, rule_id = _seed_rule(conn)
        conn.execute(
            text(
                "INSERT INTO booking_outcome "
                "(gym_account_id, rule_id, target_class, target_slot, terminal_status) "
                "VALUES (:ga, :r, 'WOD', :slot, 'granted')"
            ),
            {"ga": gym_account_id, "r": rule_id, "slot": datetime.now(UTC)},
        )
        source = conn.execute(text("SELECT outcome_source FROM booking_outcome")).scalar_one()

    assert source == "rule"


def test_skip_exclusive_check_rejects_a_skip_with_a_class_time(
    migrated_engine: Engine,
) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_booking_day_override_skip_exclusive"),
        migrated_engine.begin() as conn,
    ):
        gym_account_id, rule_id = _seed_rule(conn)
        conn.execute(
            text(
                "INSERT INTO booking_day_override "
                "(rule_id, gym_account_id, target_date, class_time, skip_day) "
                "VALUES (:r, :ga, DATE '2026-05-06', '07:00', TRUE)"
            ),
            {"r": rule_id, "ga": gym_account_id},
        )


def test_has_change_check_rejects_an_override_with_no_effect(
    migrated_engine: Engine,
) -> None:
    with (
        pytest.raises(IntegrityError, match="ck_booking_day_override_has_change"),
        migrated_engine.begin() as conn,
    ):
        gym_account_id, rule_id = _seed_rule(conn)
        conn.execute(
            text(
                "INSERT INTO booking_day_override "
                "(rule_id, gym_account_id, target_date) "
                "VALUES (:r, :ga, DATE '2026-05-06')"
            ),
            {"r": rule_id, "ga": gym_account_id},
        )


def test_unique_constraint_rejects_a_second_override_for_the_same_day(
    migrated_engine: Engine,
) -> None:
    with (
        pytest.raises(IntegrityError, match="uq_booking_day_override_rule_date"),
        migrated_engine.begin() as conn,
    ):
        gym_account_id, rule_id = _seed_rule(conn)
        for class_time in ("07:00", "09:00"):
            conn.execute(
                text(
                    "INSERT INTO booking_day_override "
                    "(rule_id, gym_account_id, target_date, class_time) "
                    "VALUES (:r, :ga, DATE '2026-05-06', :t)"
                ),
                {"r": rule_id, "ga": gym_account_id, "t": class_time},
            )


def test_deleting_the_rule_cascades_its_overrides(migrated_engine: Engine) -> None:
    with migrated_engine.begin() as conn:
        gym_account_id, rule_id = _seed_rule(conn)
        conn.execute(
            text(
                "INSERT INTO booking_day_override "
                "(rule_id, gym_account_id, target_date, class_time) "
                "VALUES (:r, :ga, DATE '2026-05-06', '07:00')"
            ),
            {"r": rule_id, "ga": gym_account_id},
        )

    with migrated_engine.begin() as conn:
        conn.execute(text("DELETE FROM scheduler_rule WHERE id = :r"), {"r": rule_id})
        remaining = conn.execute(text("SELECT COUNT(*) FROM booking_day_override")).scalar_one()

    assert remaining == 0
