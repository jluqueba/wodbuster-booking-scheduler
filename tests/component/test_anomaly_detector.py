"""Component tests for the per-run anomaly detector (US2.T2, US2.T3, CC-008).

Real Postgres so the "one open alert per (operator, kind)" partial
unique index is exercised on the upsert path. Time is scripted:
``now`` is always the fixture-controlled datetime, so a rule
seeded with a fresh ``created_at`` still looks like it existed at
the synthetic ``last_open`` moment.

The tests deliberately anchor the operator's timezone to UTC via
``WORKER_TIMEZONE`` so the ``HH:MM`` arithmetic on
:func:`next_window_open_for_rule` produces predictable numeric
results — the Madrid path is covered by the dedicated rule-jobs
suite.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.heartbeat.anomaly import (
    detect_missed_windows,
    emit_anomaly_alerts,
)
from wodbuster_worker.persistence.models import Alert, NotificationOutbox
from wodbuster_worker.scheduler.anomaly_tick import run_anomaly_tick


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture(autouse=True)
def _pin_utc_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``WORKER_TIMEZONE=UTC`` so the numeric anchors stay readable."""
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


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
    day_of_week: int = 2,  # Wed
    booking_opens_days_before: int = 2,  # Trigger Mon
    booking_opens_at: str = "21:30",
    class_type: str = "WOD",
    class_time: str = "21:30",
    active: bool = True,
    created_at: datetime | None = None,
) -> int:
    with engine.begin() as conn:
        row_id = int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule ("
                    " operator_id, day_of_week, class_type, class_time, "
                    " booking_opens_days_before, booking_opens_at, active"
                    ") VALUES ("
                    " :op, :dow, :ct, :ctime, :dbefore, :oat, :act"
                    ") RETURNING id"
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
        if created_at is not None:
            conn.execute(
                text("UPDATE scheduler_rule SET created_at = :c WHERE id = :id"),
                {"c": created_at, "id": row_id},
            )
        return row_id


def _make_outcome(
    engine: Engine,
    *,
    operator_id: int,
    rule_id: int,
    target_class: str,
    target_slot: datetime,
    attempted_at: datetime,
    terminal_status: str = "granted",
) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO booking_outcome ("
                    " operator_id, rule_id, target_class, target_slot, "
                    " attempted_at, terminal_status"
                    ") VALUES ("
                    " :op, :rule, :cls, :slot, :attempted, :status"
                    ") RETURNING id"
                ),
                {
                    "op": operator_id,
                    "rule": rule_id,
                    "cls": target_class,
                    "slot": target_slot,
                    "attempted": attempted_at,
                    "status": terminal_status,
                },
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# detect_missed_windows
# ---------------------------------------------------------------------------


def test_no_active_rules_returns_empty(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    _make_operator(postgres_engine)
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        assert detect_missed_windows(session, now=now) == []


def test_rule_with_outcome_is_not_missed(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """Executor recorded a terminal -> detector sees the row and moves on."""
    # Rule opens Mon 21:30 UTC and books Wed 21:30 UTC (2 days later).
    op_id = _make_operator(postgres_engine)
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    # Simulate: last window was Mon 2026-07-06 21:30 UTC; target slot
    # Wed 2026-07-08 21:30 UTC. The executor wrote a granted outcome.
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 8, 21, 30, tzinfo=UTC),
        attempted_at=datetime(2026, 7, 6, 21, 30, tzinfo=UTC),
    )
    # ``now`` = 30 minutes after the last window opened. Past grace,
    # inside lookback.
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)

    with session_factory() as session:
        assert detect_missed_windows(session, now=now) == []


def test_rule_without_outcome_is_missed(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """No booking_outcome row for the last elapsed window -> anomaly."""
    op_id = _make_operator(postgres_engine)
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    # 30 minutes past the last window opening.
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)

    with session_factory() as session:
        missed = detect_missed_windows(session, now=now)

    assert len(missed) == 1
    assert missed[0].rule_id == rule_id
    assert missed[0].operator_id == op_id
    assert missed[0].target_class == "WOD"
    assert missed[0].window_open == datetime(2026, 7, 6, 21, 30, tzinfo=UTC)
    assert missed[0].target_slot == datetime(2026, 7, 8, 21, 30, tzinfo=UTC)


def test_window_inside_grace_period_is_not_missed(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A window that just opened is still in-flight — no alert yet."""
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    # ``now`` = 2 minutes after the last window opened.
    now = datetime(2026, 7, 6, 21, 32, tzinfo=UTC)

    with session_factory() as session:
        # Grace of 5 minutes (default) still covers this.
        assert detect_missed_windows(session, now=now) == []


def test_window_older_than_lookback_is_ignored(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A window that fired hours ago is water under the bridge."""
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    # ``now`` = 2 hours after the last window (default lookback = 60m).
    now = datetime(2026, 7, 6, 23, 30, tzinfo=UTC)

    with session_factory() as session:
        assert detect_missed_windows(session, now=now) == []


def test_rule_created_after_window_is_not_missed(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A brand-new rule cannot have missed a window that predates it."""
    op_id = _make_operator(postgres_engine)
    # Rule created 10 minutes ago; last elapsed window opened 30
    # minutes ago (before the rule existed).
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    created_at = now - timedelta(minutes=10)
    _make_rule(postgres_engine, op_id, created_at=created_at)

    with session_factory() as session:
        assert detect_missed_windows(session, now=now) == []


def test_inactive_rules_are_ignored(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        active=False,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)

    with session_factory() as session:
        assert detect_missed_windows(session, now=now) == []


# ---------------------------------------------------------------------------
# emit_anomaly_alerts (upsert + outbox contract)
# ---------------------------------------------------------------------------


def test_emit_creates_open_alert_and_banner_row(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)

    with session_factory() as session:
        missed = detect_missed_windows(session, now=now)
        touched = emit_anomaly_alerts(session, missed, now=now)
        session.commit()

    assert len(touched) == 1
    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
        outbox = session.execute(select(NotificationOutbox)).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].kind == "heartbeat_anomaly"
    assert alerts[0].closed_at is None
    assert isinstance(alerts[0].payload, dict)
    assert alerts[0].payload["missed"][0]["rule_id"] == rule_id
    # Banner row is always emitted; no telegram row because the
    # operator has no ``telegram_chat_id`` on file.
    assert len(outbox) == 1
    assert outbox[0].kind == "banner"


def test_repeat_tick_refreshes_alert_without_duplicating(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """US2.T2: two consecutive detector ticks with the same missed
    window produce exactly one alert row (partial unique index)."""
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    first = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    second = first + timedelta(minutes=1)

    with session_factory() as session:
        missed = detect_missed_windows(session, now=first)
        emit_anomaly_alerts(session, missed, now=first)
        session.commit()

    with session_factory() as session:
        missed_again = detect_missed_windows(session, now=second)
        emit_anomaly_alerts(session, missed_again, now=second)
        session.commit()

    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
    assert len(alerts) == 1
    # last_emitted_at was refreshed on the second tick.
    assert alerts[0].last_emitted_at == second
    assert alerts[0].first_emitted_at == first


# ---------------------------------------------------------------------------
# run_anomaly_tick (end-to-end wrapper)
# ---------------------------------------------------------------------------


def test_anomaly_tick_end_to_end_creates_alert(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """CC-008: the scheduler tick alone commits alert + outbox rows."""
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)

    touched = run_anomaly_tick(session_factory, now=now)
    assert len(touched) == 1

    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
        outbox = session.execute(select(NotificationOutbox)).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].kind == "heartbeat_anomaly"
    assert len(outbox) == 1


def test_anomaly_tick_on_healthy_state_is_noop(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 8, 21, 30, tzinfo=UTC),
        attempted_at=datetime(2026, 7, 6, 21, 30, tzinfo=UTC),
    )
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)

    touched = run_anomaly_tick(session_factory, now=now)
    assert touched == []

    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
        outbox = session.execute(select(NotificationOutbox)).scalars().all()
    assert alerts == []
    assert outbox == []
