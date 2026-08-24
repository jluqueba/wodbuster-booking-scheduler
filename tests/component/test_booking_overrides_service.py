"""Component tests for the override persistence service (T-BDO-005).

Runs against real Postgres because the guards these functions enforce
(INV-001 uniqueness, FR-007 cutoff, FR-007 already-executed) are the
enforcement point of the feature's correctness, and two of them are
queries. Mocking the session would only re-implement them.

``WORKER_TIMEZONE`` is pinned to Europe/Madrid rather than UTC: the
already-executed guard converts an operator-local date into a UTC range,
which is an identity under UTC and would make the assertion vacuous.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.booking.overrides import (
    OverrideSkipConflictError,
    OverrideWindowClosedError,
    delete_override,
    edit_cutoff_for,
    load_override,
    load_overrides_in_range,
    save_override,
)
from wodbuster_worker.persistence.models import BookingDayOverride, SchedulerRule

from .conftest import gym_account_id_for

# Class on Wednesday 6 May 2026 at 18:30 Madrid; window opens Monday
# 4 May at 21:30 Madrid (19:30 UTC).
TARGET_DATE = date(2026, 5, 6)
BEFORE_CUTOFF = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture(autouse=True)
def _madrid_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")


@pytest.fixture
def make_rule(postgres_engine: Engine) -> Callable[..., SchedulerRule]:
    def _make(session: Session, *, class_type: str = "WOD") -> SchedulerRule:
        with postgres_engine.begin() as conn:
            op_id = int(
                conn.execute(
                    text("INSERT INTO operator_profile (display_name) VALUES ('Op') RETURNING id")
                ).scalar_one()
            )
            gym_account_id = gym_account_id_for(conn, op_id)
        rule = SchedulerRule(
            gym_account_id=gym_account_id,
            day_of_week=2,
            class_type=class_type,
            class_time="18:30",
            booking_opens_days_before=2,
            booking_opens_at="21:30",
            active=True,
        )
        session.add(rule)
        session.commit()
        return rule

    return _make


def _override_rows(session: Session) -> list[BookingDayOverride]:
    return list(session.execute(select(BookingDayOverride)).scalars().all())


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------


def test_saving_twice_updates_the_same_row(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    with session_factory() as session:
        rule = make_rule(session)

        save_override(
            session,
            rule=rule,
            target_date=TARGET_DATE,
            class_time="07:00",
            now=BEFORE_CUTOFF,
        )
        session.commit()
        save_override(
            session,
            rule=rule,
            target_date=TARGET_DATE,
            class_type="Endurance",
            class_time="10:00",
            validated=True,
            now=BEFORE_CUTOFF,
        )
        session.commit()

        rows = _override_rows(session)

    assert len(rows) == 1
    assert rows[0].class_type == "Endurance"
    assert rows[0].class_time == "10:00"
    assert rows[0].validated is True


def test_gym_account_is_taken_from_the_rule(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    with session_factory() as session:
        rule = make_rule(session)

        row = save_override(
            session, rule=rule, target_date=TARGET_DATE, class_time="07:00", now=BEFORE_CUTOFF
        )
        session.commit()

        assert row.gym_account_id == rule.gym_account_id


def test_saving_leaves_the_rule_row_untouched(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    with session_factory() as session:
        rule = make_rule(session)
        before = {
            "day_of_week": rule.day_of_week,
            "class_type": rule.class_type,
            "class_time": rule.class_time,
            "booking_opens_days_before": rule.booking_opens_days_before,
            "booking_opens_at": rule.booking_opens_at,
            "second_shot_class_type": rule.second_shot_class_type,
            "second_shot_class_time": rule.second_shot_class_time,
            "active": rule.active,
        }

        save_override(
            session,
            rule=rule,
            target_date=TARGET_DATE,
            class_type="Endurance",
            class_time="07:00",
            now=BEFORE_CUTOFF,
        )
        session.commit()

        reloaded = session.get(SchedulerRule, rule.id)
        assert reloaded is not None
        after = {key: getattr(reloaded, key) for key in before}

    assert after == before


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------


def test_save_one_second_past_the_cutoff_writes_nothing(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    with session_factory() as session:
        rule = make_rule(session)
        late = edit_cutoff_for(rule, TARGET_DATE) + timedelta(seconds=1)

        with pytest.raises(OverrideWindowClosedError):
            save_override(session, rule=rule, target_date=TARGET_DATE, class_time="07:00", now=late)
        session.rollback()

        assert _override_rows(session) == []


def test_save_one_second_before_the_cutoff_is_accepted(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    with session_factory() as session:
        rule = make_rule(session)
        just_in_time = edit_cutoff_for(rule, TARGET_DATE) - timedelta(seconds=1)

        save_override(
            session, rule=rule, target_date=TARGET_DATE, class_time="07:00", now=just_in_time
        )
        session.commit()

        assert len(_override_rows(session)) == 1


def test_save_is_rejected_once_the_day_already_produced_an_outcome(
    session_factory: sessionmaker[Session],
    make_rule: Callable[..., SchedulerRule],
    postgres_engine: Engine,
) -> None:
    with session_factory() as session:
        rule = make_rule(session)
        with postgres_engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO booking_outcome ("
                    " gym_account_id, rule_id, target_class, target_slot, "
                    " attempted_at, terminal_status"
                    ") VALUES (:ga, :rule, 'WOD', :slot, :att, 'granted')"
                ),
                {
                    "ga": rule.gym_account_id,
                    "rule": rule.id,
                    # 18:30 Madrid on the target date = 16:30 UTC.
                    "slot": datetime(2026, 5, 6, 16, 30, tzinfo=UTC),
                    "att": BEFORE_CUTOFF,
                },
            )

        with pytest.raises(OverrideWindowClosedError):
            save_override(
                session,
                rule=rule,
                target_date=TARGET_DATE,
                class_time="07:00",
                now=BEFORE_CUTOFF,
            )
        session.rollback()

        assert _override_rows(session) == []


# ---------------------------------------------------------------------------
# Load and delete
# ---------------------------------------------------------------------------


def test_delete_removes_the_row_then_reports_nothing_to_do(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    with session_factory() as session:
        rule = make_rule(session)
        save_override(
            session, rule=rule, target_date=TARGET_DATE, class_time="07:00", now=BEFORE_CUTOFF
        )
        session.commit()

        assert delete_override(session, rule=rule, target_date=TARGET_DATE, now=BEFORE_CUTOFF)
        session.commit()
        assert not delete_override(session, rule=rule, target_date=TARGET_DATE, now=BEFORE_CUTOFF)
        session.commit()

        assert _override_rows(session) == []


def test_delete_past_the_cutoff_is_rejected(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    with session_factory() as session:
        rule = make_rule(session)
        save_override(
            session, rule=rule, target_date=TARGET_DATE, class_time="07:00", now=BEFORE_CUTOFF
        )
        session.commit()
        late = edit_cutoff_for(rule, TARGET_DATE) + timedelta(seconds=1)

        with pytest.raises(OverrideWindowClosedError):
            delete_override(session, rule=rule, target_date=TARGET_DATE, now=late)
        session.rollback()

        assert len(_override_rows(session)) == 1


def test_load_override_finds_only_the_matching_date(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    with session_factory() as session:
        rule = make_rule(session)
        save_override(
            session, rule=rule, target_date=TARGET_DATE, class_time="07:00", now=BEFORE_CUTOFF
        )
        session.commit()

        assert load_override(session, rule_id=int(rule.id), target_date=TARGET_DATE) is not None
        assert load_override(session, rule_id=int(rule.id), target_date=date(2026, 5, 13)) is None


def test_load_overrides_in_range_keys_by_rule_and_date(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    with session_factory() as session:
        rule = make_rule(session)
        save_override(
            session, rule=rule, target_date=TARGET_DATE, class_time="07:00", now=BEFORE_CUTOFF
        )
        save_override(
            session,
            rule=rule,
            target_date=date(2026, 5, 13),
            class_time="08:00",
            now=BEFORE_CUTOFF,
        )
        session.commit()

        loaded = load_overrides_in_range(
            session,
            gym_account_id=int(rule.gym_account_id),
            start=date(2026, 5, 1),
            end=date(2026, 5, 7),
        )

    assert list(loaded) == [(int(rule.id), TARGET_DATE)]
    assert loaded[(int(rule.id), TARGET_DATE)].class_time == "07:00"


# ---------------------------------------------------------------------------
# Skipping a day (T-BDO-010)
# ---------------------------------------------------------------------------


def test_skip_persists_with_no_alternative_target(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    """FR-003: a skip carries the mark and nothing else."""
    with session_factory() as session:
        rule = make_rule(session)

        save_override(session, rule=rule, target_date=TARGET_DATE, skip_day=True, now=BEFORE_CUTOFF)
        session.commit()

        rows = _override_rows(session)

    assert len(rows) == 1
    assert rows[0].skip_day is True
    assert rows[0].class_type is None
    assert rows[0].class_time is None


def test_skip_carrying_a_target_is_refused_before_the_database(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    """INV-002: the service is the first line of defence, so the user gets a
    message rather than an integrity error."""
    with session_factory() as session:
        rule = make_rule(session)

        for kwargs in ({"class_time": "07:00"}, {"class_type": "Endurance"}):
            with pytest.raises(OverrideSkipConflictError):
                save_override(
                    session,
                    rule=rule,
                    target_date=TARGET_DATE,
                    skip_day=True,
                    now=BEFORE_CUTOFF,
                    **kwargs,
                )
        session.rollback()

        assert _override_rows(session) == []


def test_the_check_constraint_backs_the_service_up(
    session_factory: sessionmaker[Session],
    make_rule: Callable[..., SchedulerRule],
) -> None:
    """INV-002: ``ck_booking_day_override_skip_exclusive`` still rejects the
    combination when it is written around the service."""
    with session_factory() as session:
        rule = make_rule(session)
        rule_id = int(rule.id)
        gym_account_id = int(rule.gym_account_id)

        with pytest.raises(IntegrityError) as excinfo:
            session.execute(
                text(
                    "INSERT INTO booking_day_override "
                    "(rule_id, gym_account_id, target_date, class_time, skip_day) "
                    "VALUES (:r, :ga, :d, '07:00', true)"
                ),
                {"r": rule_id, "ga": gym_account_id, "d": TARGET_DATE},
            )
        session.rollback()

    assert "ck_booking_day_override_skip_exclusive" in str(excinfo.value)


def test_switching_a_time_override_to_a_skip_clears_both_fields(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    """T-BDO-010 acceptance 3: one upsert, not a delete and an insert."""
    with session_factory() as session:
        rule = make_rule(session)
        save_override(
            session,
            rule=rule,
            target_date=TARGET_DATE,
            class_type="Endurance",
            class_time="07:00",
            now=BEFORE_CUTOFF,
        )
        session.commit()

        save_override(session, rule=rule, target_date=TARGET_DATE, skip_day=True, now=BEFORE_CUTOFF)
        session.commit()

        rows = _override_rows(session)

    assert len(rows) == 1
    assert rows[0].skip_day is True
    assert rows[0].class_type is None
    assert rows[0].class_time is None


def test_reverting_a_skip_removes_the_row(
    session_factory: sessionmaker[Session], make_rule: Callable[..., SchedulerRule]
) -> None:
    """FR-022, CC-015: a skip is undone the same way any override is."""
    with session_factory() as session:
        rule = make_rule(session)
        save_override(session, rule=rule, target_date=TARGET_DATE, skip_day=True, now=BEFORE_CUTOFF)
        session.commit()

        assert delete_override(session, rule=rule, target_date=TARGET_DATE, now=BEFORE_CUTOFF)
        session.commit()

        assert load_override(session, rule_id=int(rule.id), target_date=TARGET_DATE) is None
