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

from .conftest import gym_account_id_for


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
        op_id = int(
            conn.execute(
                text("INSERT INTO operator_profile (display_name) VALUES (:n) RETURNING id"),
                {"n": name},
            ).scalar_one()
        )
        conn.execute(
            text(
                "INSERT INTO gym_account (user_id, gym_slug, display_name, idu) "
                "VALUES (:op, 'antworktrainingcenter', :n, :idu)"
            ),
            {"op": op_id, "n": name, "idu": f"idu{op_id:032d}"[:32]},
        )
        return op_id


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
        gym_account_id = gym_account_id_for(conn, operator_id)
        row_id = int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule ("
                    " gym_account_id, day_of_week, class_type, class_time, "
                    " booking_opens_days_before, booking_opens_at, active"
                    ") VALUES ("
                    " :op, :dow, :ct, :ctime, :dbefore, :oat, :act"
                    ") RETURNING id"
                ),
                {
                    "op": gym_account_id,
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
        gym_account_id = gym_account_id_for(conn, operator_id)
        return int(
            conn.execute(
                text(
                    "INSERT INTO booking_outcome ("
                    " gym_account_id, rule_id, target_class, target_slot, "
                    " attempted_at, terminal_status"
                    ") VALUES ("
                    " :op, :rule, :cls, :slot, :attempted, :status"
                    ") RETURNING id"
                ),
                {
                    "op": gym_account_id,
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
        ga_id = gym_account_id_for(session, op_id)

    assert len(missed) == 1
    assert missed[0].rule_id == rule_id
    assert missed[0].gym_account_id == ga_id
    assert missed[0].target_class == "WOD"
    assert missed[0].window_open == datetime(2026, 7, 6, 21, 30, tzinfo=UTC)
    assert missed[0].target_slot == datetime(2026, 7, 8, 21, 30, tzinfo=UTC)


def test_override_moving_the_class_time_is_not_missed(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """ADR-0012: a single-day override books, and records, another slot.

    The executor resolves ``target_slot`` from the override's
    ``class_time``, so the outcome row lands on the same day but at a
    different instant than the rule's own. Matching on the instant
    reported those successful runs as silent.
    """
    op_id = _make_operator(postgres_engine)
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    # Rule targets Wed 21:30 UTC; the override moved the class to 19:00.
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 8, 19, 0, tzinfo=UTC),
        attempted_at=datetime(2026, 7, 6, 21, 30, tzinfo=UTC),
    )
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)

    with session_factory() as session:
        assert detect_missed_windows(session, now=now) == []


def test_outcome_on_another_day_does_not_count(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """Day-scoped evidence stays scoped: the previous week is not proof."""
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
        target_slot=datetime(2026, 7, 1, 21, 30, tzinfo=UTC),
        attempted_at=datetime(2026, 6, 29, 21, 30, tzinfo=UTC),
    )
    now = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)

    with session_factory() as session:
        missed = detect_missed_windows(session, now=now)

    assert len(missed) == 1
    assert missed[0].target_slot == datetime(2026, 7, 8, 21, 30, tzinfo=UTC)


def test_dst_spring_forward_day_is_23_hours_wide(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 23-hour local day bounds the evidence window to 23 hours.

    2026-03-29 is the spring-forward Sunday in Europe/Madrid: local
    midnight is 23:00 UTC the previous day and the day ends at 22:00
    UTC, not 23:00. Both edges are asserted, so a range computed as a
    flat 24 hours from local midnight fails this test by accepting an
    outcome that belongs to the following day.
    """
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")
    op_id = _make_operator(postgres_engine)
    # Window opens Sunday 09:00 local, class the same day at 21:00 local.
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        day_of_week=6,
        booking_opens_days_before=0,
        booking_opens_at="09:00",
        class_time="21:00",
        created_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    )
    now = datetime(2026, 3, 29, 7, 30, tzinfo=UTC)  # 30 min past the window

    # 22:30 UTC is 00:30 local on the 30th: the next day, past the end
    # edge. Not evidence.
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 3, 29, 22, 30, tzinfo=UTC),
        attempted_at=now,
    )
    with session_factory() as session:
        assert len(detect_missed_windows(session, now=now)) == 1

    # 23:15 UTC on the 28th is 00:15 local on the 29th: the start edge
    # of the short day. Evidence.
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 3, 28, 23, 15, tzinfo=UTC),
        attempted_at=now,
    )
    with session_factory() as session:
        assert detect_missed_windows(session, now=now) == []


def test_dst_autumn_back_day_is_25_hours_wide(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 25-hour local day bounds the evidence window to 25 hours.

    2026-10-25 is the autumn-back Sunday in Europe/Madrid: local
    midnight is 22:00 UTC the previous day and the day runs until 23:00
    UTC. A range computed as a flat 24 hours fails this test by
    rejecting an outcome recorded in the day's final hour.
    """
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")
    op_id = _make_operator(postgres_engine)
    # Window opens Sunday 09:00 local, class the same day at 23:30 local.
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        day_of_week=6,
        booking_opens_days_before=0,
        booking_opens_at="09:00",
        class_time="23:30",
        created_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
    )
    now = datetime(2026, 10, 25, 8, 30, tzinfo=UTC)  # 30 min past the window

    # 23:15 UTC is 00:15 local on the 26th: past the end edge.
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 10, 25, 23, 15, tzinfo=UTC),
        attempted_at=now,
    )
    with session_factory() as session:
        assert len(detect_missed_windows(session, now=now)) == 1

    # 22:45 UTC is 23:45 local on the 25th: inside the long day, and an
    # hour past where a flat 24-hour range would have cut it off.
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 10, 25, 22, 45, tzinfo=UTC),
        attempted_at=now,
    )
    with session_factory() as session:
        assert detect_missed_windows(session, now=now) == []


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


def test_repeat_tick_suppresses_duplicate_notifications(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """US2.T2: two consecutive detector ticks with the same missed
    window produce exactly one alert row (partial unique index) and
    exactly one round of notifications.

    The tick runs every 60 seconds and a missed window stays
    detectable for the whole lookback, so re-emitting per tick meant
    an hour of one notification per minute for a single silent run.
    """
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
        touched = emit_anomaly_alerts(session, missed_again, now=second)
        session.commit()

    # Still detected, deliberately not re-notified.
    assert len(missed_again) == 1
    assert touched == []

    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
        outbox = session.execute(select(NotificationOutbox)).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].first_emitted_at == first
    # ``last_emitted_at`` tracks the last notification, not the last
    # detection, because it is what gates the next one.
    assert alerts[0].last_emitted_at == first
    assert len(outbox) == 1


def test_refire_interval_elapsed_renotifies(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A still-missing window notifies again once the interval passes."""
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    first = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    second = datetime(2026, 7, 6, 22, 20, tzinfo=UTC)
    interval = timedelta(minutes=10)

    run_anomaly_tick(session_factory, now=first, refire_interval=interval)
    touched = run_anomaly_tick(session_factory, now=second, refire_interval=interval)

    assert len(touched) == 1
    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
        outbox = session.execute(select(NotificationOutbox)).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].last_emitted_at == second
    assert len(outbox) == 2


def test_unreported_window_notifies_inside_refire_interval(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A second silent run is never swallowed by the re-fire gate."""
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        booking_opens_at="21:30",
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    # Second rule on the same day whose window opens 22 minutes later,
    # so the first tick still has it inside the grace period.
    _make_rule(
        postgres_engine,
        op_id,
        booking_opens_at="21:52",
        class_type="OPEN BOX",
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    first = datetime(2026, 7, 6, 21, 56, tzinfo=UTC)
    second = datetime(2026, 7, 6, 21, 58, tzinfo=UTC)

    run_anomaly_tick(session_factory, now=first)
    touched = run_anomaly_tick(session_factory, now=second)

    assert len(touched) == 1
    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
        outbox = session.execute(select(NotificationOutbox)).scalars().all()
    assert len(alerts) == 1
    assert len(alerts[0].payload["missed"]) == 2
    assert len(outbox) == 2


# ---------------------------------------------------------------------------
# close_resolved_anomalies
# ---------------------------------------------------------------------------


def test_tracked_window_survives_a_later_detection(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A silent run stays tracked until it resolves or expires.

    The detector only looks back an hour, so a later tick reports a
    different window without meaning the earlier one recovered. Payload
    entries accumulate; resolving one must not clear the banner for the
    other.
    """
    op_id = _make_operator(postgres_engine)
    first_rule = _make_rule(
        postgres_engine,
        op_id,
        booking_opens_at="21:30",
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    second_rule = _make_rule(
        postgres_engine,
        op_id,
        booking_opens_at="22:40",
        class_type="OPEN BOX",
        class_time="22:40",
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )

    run_anomaly_tick(session_factory, now=datetime(2026, 7, 6, 22, 0, tzinfo=UTC))
    # 23:00: the first window fell out of the lookback (90 minutes old)
    # and only the second one is detected.
    run_anomaly_tick(session_factory, now=datetime(2026, 7, 6, 23, 0, tzinfo=UTC))

    with session_factory() as session:
        alert = session.execute(select(Alert)).scalars().one()
        tracked = {entry["rule_id"] for entry in alert.payload["missed"]}
    assert tracked == {first_rule, second_rule}

    # The second rule's booking lands late; the first is still silent.
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=second_rule,
        target_class="OPEN BOX",
        target_slot=datetime(2026, 7, 8, 22, 40, tzinfo=UTC),
        attempted_at=datetime(2026, 7, 6, 23, 2, tzinfo=UTC),
    )
    run_anomaly_tick(session_factory, now=datetime(2026, 7, 6, 23, 5, tzinfo=UTC))

    with session_factory() as session:
        alert = session.execute(select(Alert)).scalars().one()
    assert alert.closed_at is None
    assert [entry["rule_id"] for entry in alert.payload["missed"]] == [first_rule]


def test_alert_closes_when_the_outcome_lands(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A late outcome clears the banner without operator action."""
    op_id = _make_operator(postgres_engine)
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    first = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    run_anomaly_tick(session_factory, now=first)

    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 8, 21, 30, tzinfo=UTC),
        attempted_at=datetime(2026, 7, 6, 22, 1, tzinfo=UTC),
    )
    second = first + timedelta(minutes=2)
    run_anomaly_tick(session_factory, now=second)

    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].closed_at == second


def test_alert_closes_once_the_window_ages_out(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """Nothing can be done about a booking window that closed a day ago."""
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    first = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    run_anomaly_tick(session_factory, now=first)

    later = datetime(2026, 7, 7, 23, 0, tzinfo=UTC)  # window opened 25.5h ago
    run_anomaly_tick(session_factory, now=later)

    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
        outbox = session.execute(select(NotificationOutbox)).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].closed_at == later
    # Closing is not an event the operator is notified about.
    assert len(outbox) == 1


def test_still_missing_window_stays_open(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """The close pass never clears an alert the detector still raises."""
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        created_at=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
    )
    first = datetime(2026, 7, 6, 22, 0, tzinfo=UTC)
    run_anomaly_tick(session_factory, now=first)
    run_anomaly_tick(session_factory, now=first + timedelta(minutes=1))

    with session_factory() as session:
        alerts = session.execute(select(Alert)).scalars().all()
    assert len(alerts) == 1
    assert alerts[0].closed_at is None


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
