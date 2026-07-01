"""Foundational tests for the Alembic baseline migration (F4.T2).

Runs ``alembic upgrade head`` programmatically against a temp SQLite
file, then:

- asserts every one of the ten declared tables exists on disk;
- inserts a minimal row into each and reads it back, exercising the
  concrete column types (LargeBinary, DateTime, Enum) end-to-end.

These are the load-bearing checks that keep the baseline migration
honest against ``persistence.models``. Autogenerate diffs and schema
drift show up here first.
"""

from __future__ import annotations

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


@pytest.fixture
def migrated_engine(tmp_path: Path) -> Engine:
    """Yield an engine bound to a freshly migrated temp database.

    Uses an on-disk file (not ``:memory:``) so the alembic version
    table survives across connections, matching production behaviour
    on the Azure Files mount.
    """
    db_path = tmp_path / "migrated.db"
    url = f"sqlite:///{db_path.as_posix()}"

    cfg = Config(str(_ALEMBIC_INI))
    cfg.set_main_option("sqlalchemy.url", url)
    command.upgrade(cfg, "head")

    engine = create_engine(url, future=True)
    try:
        # Foreign keys must be on to satisfy the inserts below.
        with engine.begin() as conn:
            conn.execute(text("PRAGMA foreign_keys=ON"))
        yield engine
    finally:
        engine.dispose()


def test_upgrade_creates_every_domain_table(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    actual = set(insp.get_table_names())
    actual.discard("alembic_version")

    assert actual == EXPECTED_TABLES


def test_alert_partial_unique_index_present(migrated_engine: Engine) -> None:
    insp = inspect(migrated_engine)
    names = {ix["name"] for ix in insp.get_indexes("alert")}

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
    now = datetime.now(UTC).isoformat()

    with migrated_engine.begin() as conn:
        # Foreign key roots first.
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
                "VALUES (:op, 1, 48, 1) RETURNING id"
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
            {"op": op_id, "s": now, "e": now},
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
                "VALUES (:op, 'telegram', 'chat-1', '{}')"
            ),
            {"op": op_id},
        )

    with migrated_engine.connect() as conn:
        for table in EXPECTED_TABLES:
            count = conn.execute(
                text(f"SELECT COUNT(*) FROM {table}")
            ).scalar_one()
            assert count >= 1, f"expected at least one row in {table}, got {count}"

        stored_ct = conn.execute(
            text("SELECT cookie_ciphertext FROM cookie_credential")
        ).scalar_one()
        assert stored_ct == b"\x00\x01"
