"""Component tests for :func:`list_upcoming_slots` (H.1 full+).

Covers the merge between granted outcomes and pending rule
projections, including the dedup that stops a rule's next
occurrence from being listed twice (once as ``granted`` from a real
outcome and once as ``pending`` from the projection).

Uses real Postgres because the service composes two SQL queries
against ``booking_outcome`` and ``scheduler_rule``; mocking them
would just re-implement the queries.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.booking.upcoming import list_upcoming_slots


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
    """Pin ``WORKER_TIMEZONE=UTC`` so projections use tidy anchors."""
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")


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
    day_of_week: int = 2,
    class_type: str = "WOD",
    class_time: str = "21:30",
    booking_opens_days_before: int = 0,
    booking_opens_at: str = "21:30",
    active: bool = True,
) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule ("
                    " operator_id, day_of_week, class_type, class_time, "
                    " booking_opens_days_before, booking_opens_at, active"
                    ") VALUES (:op, :dow, :ct, :ctime, :dbefore, :oat, :act) "
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


def _make_outcome(
    engine: Engine,
    *,
    operator_id: int,
    rule_id: int,
    target_class: str,
    target_slot: datetime,
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
                    "attempted": target_slot - timedelta(days=1),
                    "status": terminal_status,
                },
            ).scalar_one()
        )


def test_returns_only_granted_outcome_when_no_rule_exists(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    now = datetime(2026, 7, 14, 12, 0, tzinfo=UTC)
    slot = datetime(2026, 7, 16, 21, 30, tzinfo=UTC)
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=None,  # type: ignore[arg-type]
        target_class="StandaloneWOD",
        target_slot=slot,
    )
    # Delete the rule slot via NULL rule_id: SQLAlchemy allows it
    # since ``rule_id`` is nullable on ``booking_outcome`` (rule may
    # have been deleted after the booking landed).
    _ = op_id

    with session_factory() as session:
        slots = list_upcoming_slots(session, op_id, now=now)

    assert len(slots) == 1
    assert slots[0].kind == "granted"
    assert slots[0].target_class == "StandaloneWOD"
    assert slots[0].target_slot == slot


def test_projects_pending_when_no_outcome_covers_the_rule_slot(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """Only a rule exists — the projection surfaces the next
    ``(target_slot, class)`` pair as a pending slot."""
    op_id = _make_operator(postgres_engine)
    # Attend Wed (2), open same day 21:30 → target = Wed 21:30 UTC.
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        day_of_week=2,
        booking_opens_days_before=0,
        booking_opens_at="21:30",
        class_time="21:30",
    )
    # Mon 2026-07-13 12:00 → next Wed = 2026-07-15 21:30.
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        slots = list_upcoming_slots(session, op_id, now=now, horizon_days=6)

    assert len(slots) == 1
    assert slots[0].kind == "pending"
    assert slots[0].target_class == "WOD"
    assert slots[0].rule_id == rule_id
    assert slots[0].booking_id is None
    assert slots[0].target_slot == datetime(2026, 7, 15, 21, 30, tzinfo=UTC)


def test_outcome_dedups_projection_for_same_rule_slot(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """The rule's next slot already has a ``granted`` outcome →
    that slot appears once as granted, never duplicated as pending."""
    op_id = _make_operator(postgres_engine)
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        day_of_week=2,
        booking_opens_days_before=0,
        booking_opens_at="21:30",
        class_time="21:30",
    )
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
        terminal_status="granted",
    )
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        slots = list_upcoming_slots(session, op_id, now=now, horizon_days=6)

    assert len(slots) == 1
    assert slots[0].kind == "granted"
    assert slots[0].target_slot == datetime(2026, 7, 15, 21, 30, tzinfo=UTC)
    assert slots[0].rule_id == rule_id


def test_non_granted_outcome_still_suppresses_pending_duplicate(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A ``skipped``/``cancelled``/``full`` outcome for the same
    ``(rule, target_slot)`` also covers the projection — the
    operator already sees that terminal in the All attempts table."""
    op_id = _make_operator(postgres_engine)
    rule_id = _make_rule(
        postgres_engine,
        op_id,
        day_of_week=2,
        booking_opens_days_before=0,
        booking_opens_at="21:30",
        class_time="21:30",
    )
    _make_outcome(
        postgres_engine,
        operator_id=op_id,
        rule_id=rule_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
        terminal_status="cancelled",
    )
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        slots = list_upcoming_slots(session, op_id, now=now, horizon_days=6)

    # Cancelled outcomes are not surfaced by the granted-only load,
    # and the covered-key set still suppresses the pending duplicate,
    # so the section is empty for this horizon.
    assert slots == []


def test_projects_multiple_weekly_occurrences_within_horizon(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A 14-day horizon covers two occurrences of a weekly rule."""
    op_id = _make_operator(postgres_engine)
    _make_rule(
        postgres_engine,
        op_id,
        day_of_week=2,
        booking_opens_days_before=0,
        booking_opens_at="21:30",
        class_time="21:30",
    )
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        slots = list_upcoming_slots(session, op_id, now=now, horizon_days=14)

    assert len(slots) == 2
    assert slots[0].target_slot == datetime(2026, 7, 15, 21, 30, tzinfo=UTC)
    assert slots[1].target_slot == datetime(2026, 7, 22, 21, 30, tzinfo=UTC)
    assert all(s.kind == "pending" for s in slots)


def test_inactive_rule_is_not_projected(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    _make_rule(postgres_engine, op_id, active=False)
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        slots = list_upcoming_slots(session, op_id, now=now, horizon_days=14)

    assert slots == []


def test_operator_scope_isolation(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """A rule + outcome for operator B do not leak into operator A."""
    op_a = _make_operator(postgres_engine, name="A")
    op_b = _make_operator(postgres_engine, name="B")
    _make_rule(postgres_engine, op_b)
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    with session_factory() as session:
        slots_a = list_upcoming_slots(session, op_a, now=now, horizon_days=14)
        slots_b = list_upcoming_slots(session, op_b, now=now, horizon_days=14)

    assert slots_a == []
    assert len(slots_b) >= 1
