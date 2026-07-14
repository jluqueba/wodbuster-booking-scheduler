"""Component tests for :func:`compute_next_window` (rule model v2).

Uses the real Postgres via ``postgres_engine`` — the lookahead is
schema-driven and mocking would just re-implement the query.

Rule-model-v2 semantics: ``day_of_week`` is the *attendance* day. The
booking window opens on ``(day_of_week - booking_opens_days_before)
mod 7`` at ``booking_opens_at``. That trigger instant is what the
function returns.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.heartbeat.next_window import (
    compute_next_booking,
    compute_next_window,
)


@pytest.fixture(autouse=True)
def _pin_utc_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``WORKER_TIMEZONE=UTC`` so numeric assertions stay readable.

    The scheduler interprets every rule's ``HH:MM`` in the operator
    zone (default ``Europe/Madrid``). These component tests were
    written against UTC; pinning here keeps them independent of the
    active DST offset. The Madrid path is covered by the unit tests.
    """
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")


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
                text("INSERT INTO operator_profile (display_name) VALUES (:n) RETURNING id"),
                {"n": name},
            ).scalar_one()
        )


def _make_rule(
    engine: Engine,
    operator_id: int,
    *,
    day_of_week: int,
    booking_opens_days_before: int,
    booking_opens_at: str = "21:30",
    class_type: str = "WOD",
    class_time: str = "21:30",
    active: bool = True,
) -> int:
    """Insert a scheduler_rule row; return the id."""
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule "
                    "(operator_id, day_of_week, class_type, class_time, "
                    "booking_opens_days_before, booking_opens_at, active) "
                    "VALUES (:op, :dow, :ct, :ctime, :dbefore, :oat, :act) "
                    "RETURNING id"
                ),
                {
                    "op": operator_id,
                    "dow": day_of_week,
                    "ct": class_type,
                    "ctime": class_time,
                    "dbefore": booking_opens_days_before,
                    "oat": booking_opens_at,
                    "act": active,
                },
            ).scalar_one()
        )


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

    # Rule A: attend Thursday (3), opens 2d before at 21:30
    # → trigger day = (3-2)%7 = 1 (Tuesday). Next Tuesday is
    # 2026-07-14 21:30.
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=3,
        booking_opens_days_before=2,
        booking_opens_at="21:30",
    )
    # Rule B: attend Friday (4), opens 1d before at 07:30
    # → trigger day = (4-1)%7 = 3 (Thursday). Next Thursday is
    # 2026-07-09 07:30.
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=4,
        booking_opens_days_before=1,
        booking_opens_at="07:30",
    )

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    # Rule B (2026-07-09 07:30) is earlier than Rule A (2026-07-14
    # 21:30). Expect B.
    assert result == datetime(2026, 7, 9, 7, 30, tzinfo=UTC)


def test_inactive_rules_are_ignored(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    # Inactive rule that would otherwise win: attend Thu, opens 1d
    # before at 14:00 → trigger Wed 14:00 (today, still in future).
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=3,
        booking_opens_days_before=1,
        booking_opens_at="14:00",
        active=False,
    )
    # Active rule further out: attend Sat (5), opens 1d before at
    # 10:00 → trigger Fri 10:00 → 2026-07-10 10:00.
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=5,
        booking_opens_days_before=1,
        booking_opens_at="10:00",
    )

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    assert result == datetime(2026, 7, 10, 10, 0, tzinfo=UTC)


def test_same_day_future_window_returns_today(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # 2026-07-08 Wed 06:00 UTC. Rule attends Wed, opens 0d before at
    # 15:30 → trigger Wed 15:30 (today, still future).
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 6, 0, tzinfo=UTC)
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=2,
        booking_opens_days_before=0,
        booking_opens_at="15:30",
    )

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
    op_a = _make_operator(postgres_engine, name="A")
    op_b = _make_operator(postgres_engine, name="B")
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    # Op B: attend Thursday, opens 1d before at 20:30 → trigger Wed
    # 20:30 (today, later).
    _make_rule(
        postgres_engine,
        op_b,
        day_of_week=3,
        booking_opens_days_before=1,
        booking_opens_at="20:30",
    )

    with session_factory() as session:
        assert compute_next_window(session, op_a, now) is None
        assert compute_next_window(session, op_b, now) == datetime(2026, 7, 8, 20, 30, tzinfo=UTC)


def test_zero_days_before_uses_same_day_as_attendance(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """``opens_days_before=0`` means the window opens on the class day itself."""
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    # Attend Fri (4), opens 0d before at 18:00 → trigger Fri 18:00.
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=4,
        booking_opens_days_before=0,
        booking_opens_at="18:00",
    )

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    assert result == datetime(2026, 7, 10, 18, 0, tzinfo=UTC)


def test_rolls_forward_when_first_occurrence_is_before_now(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # Wed 12:00. Attend Wed, opens 0d before at 08:00 → trigger Wed
    # 08:00 (already passed). Roll forward one week.
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=2,
        booking_opens_days_before=0,
        booking_opens_at="08:00",
    )

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    assert result == now.replace(hour=8) + timedelta(days=7)


def test_days_before_wraps_across_week_boundary(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """Attending Mon with a 3-day lead fires the previous Friday."""
    # Wed 2026-07-08 12:00.
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    # Attend Mon (0), opens 3d before at 22:40
    # → trigger day = (0-3)%7 = 4 (Friday). Next Friday is 2026-07-10
    # at 22:40.
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=0,
        booking_opens_days_before=3,
        booking_opens_at="22:40",
    )

    with session_factory() as session:
        result = compute_next_window(session, op_id, now)

    assert result == datetime(2026, 7, 10, 22, 40, tzinfo=UTC)


def test_compute_next_booking_returns_window_target_and_rule_id(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """The dashboard-side helper reports both the fire time and the
    class slot the rule is aiming at."""
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    # Attend Wednesday (2), opens 2 days before (Monday) at 21:30,
    # class starts at 21:30. Class Wed 2026-07-15 21:30 UTC (fixture
    # pins WORKER_TIMEZONE=UTC); window opens Mon 2026-07-13 21:30.
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        day_of_week=2,
        booking_opens_days_before=2,
        booking_opens_at="21:30",
        class_time="21:30",
    )

    with session_factory() as session:
        result = compute_next_booking(session, op_id, now)

    assert result is not None
    assert result.window_open == datetime(2026, 7, 13, 21, 30, tzinfo=UTC)
    assert result.target_slot == datetime(2026, 7, 15, 21, 30, tzinfo=UTC)
    assert result.rule_id == rule_id


def test_compute_next_booking_none_when_no_active_rules(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        assert compute_next_booking(session, op_id, now) is None
