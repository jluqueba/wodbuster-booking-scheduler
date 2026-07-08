"""Unit tests for :func:`compute_next_window` (US4.T2).

Uses the real Postgres via ``postgres_engine`` — the lookahead is a
SQL-driven function and mocking would just re-implement the query.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.heartbeat.next_window import compute_next_window


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def _make_operator(engine: Engine, name: str = "Op") -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO operator_profile (display_name) "
                    "VALUES (:n) RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def _make_rule(
    engine: Engine,
    operator_id: int,
    *,
    day_of_week: int,
    window_offset_hours: int,
    time_slot: str = "21:30",
    class_type: str = "WOD",
    active: bool = True,
) -> int:
    """Insert a scheduler_rule and one class_preference; return the rule id."""
    with engine.begin() as conn:
        rule_id = int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule "
                    "(operator_id, day_of_week, window_offset_hours, active) "
                    "VALUES (:op, :dow, :off, :act) RETURNING id"
                ),
                {
                    "op": operator_id,
                    "dow": day_of_week,
                    "off": window_offset_hours,
                    "act": active,
                },
            ).scalar_one()
        )
        conn.execute(
            text(
                "INSERT INTO class_preference "
                "(rule_id, order_index, class_type, target_time_slot) "
                "VALUES (:r, 0, :c, :t)"
            ),
            {"r": rule_id, "c": class_type, "t": time_slot},
        )
    return rule_id


def test_no_rules_returns_none(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        assert compute_next_window(session, op_id, now) is None


def test_returns_earliest_window_across_active_rules(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # 2026-07-08 is a Wednesday (weekday=2).
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    # Rule A: Thursday 21:30, opens 48h before class start => Tue 21:30.
    # Next Thursday is 2026-07-09 (weekday 3), class 2026-07-09 21:30,
    # window opens 2026-07-07 21:30. That is in the past, so the
    # function should roll forward one week to 2026-07-14 21:30.
    _make_rule(postgres_engine, op_id, day_of_week=3, window_offset_hours=48)
    # Rule B: Friday 07:30, opens 24h before => Thu 07:30. Next Friday
    # is 2026-07-10; class 2026-07-10 07:30; window opens 2026-07-09
    # 07:30. That is in the future.
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=4,
        window_offset_hours=24,
        time_slot="07:30",
    )

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    # Rule B's window (2026-07-09 07:30) is earlier than Rule A's
    # rolled-forward window (2026-07-14 21:30). Expect Rule B.
    assert result == datetime(2026, 7, 9, 7, 30, tzinfo=UTC)


def test_inactive_rules_are_ignored(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    # Inactive rule that would otherwise win.
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=3,  # tomorrow
        window_offset_hours=1,
        time_slot="14:00",
        active=False,
    )
    # Active rule further out.
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=5,  # Saturday
        window_offset_hours=24,
        time_slot="10:00",
    )

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    # Saturday 10:00 minus 24h = Friday 10:00.
    assert result == datetime(2026, 7, 10, 10, 0, tzinfo=UTC)


def test_rule_without_preferences_is_skipped(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    # Insert a rule with no preferences.
    with postgres_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO scheduler_rule "
                "(operator_id, day_of_week, window_offset_hours, active) "
                "VALUES (:op, 3, 24, true)"
            ),
            {"op": op_id},
        )

    with session_factory() as session:
        assert compute_next_window(session, op_id, now) is None


def test_same_day_future_window_returns_today(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # 2026-07-08 Wed 06:00 UTC. Rule Wed 21:30 with 6h offset -> window
    # opens Wed 15:30 (today, still future).
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 6, 0, tzinfo=UTC)
    _make_rule(postgres_engine, op_id, day_of_week=2, window_offset_hours=6)

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    assert result == datetime(2026, 7, 8, 15, 30, tzinfo=UTC)


def test_naive_datetime_raises() -> None:
    from unittest.mock import MagicMock

    with pytest.raises(ValueError, match="timezone-aware"):
        compute_next_window(MagicMock(), 1, datetime(2026, 7, 8, 12, 0))


def test_operator_scope_isolation(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Rules on operator B must not affect operator A's lookahead.
    op_a = _make_operator(postgres_engine, name="A")
    op_b = _make_operator(postgres_engine, name="B")
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    _make_rule(postgres_engine, op_b, day_of_week=3, window_offset_hours=1)  # tomorrow

    with session_factory() as session:
        assert compute_next_window(session, op_a, now) is None
        assert compute_next_window(session, op_b, now) == datetime(
            2026, 7, 9, 20, 30, tzinfo=UTC
        )


def test_offset_zero_returns_class_start(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # window_offset_hours=0 -> window opens at class start.
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=4,  # Friday
        window_offset_hours=0,
        time_slot="18:00",
    )

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    assert result == datetime(2026, 7, 10, 18, 0, tzinfo=UTC)


def test_rolls_forward_when_first_occurrence_is_before_now(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Wednesday 12:00. Rule for Wednesday 08:00 with 0h offset -> class
    # was earlier today, window in the past. Should roll forward one week.
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=2,  # Wed
        window_offset_hours=0,
        time_slot="08:00",
    )

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    # Next Wednesday.
    assert result == now.replace(hour=8) + timedelta(days=7)
