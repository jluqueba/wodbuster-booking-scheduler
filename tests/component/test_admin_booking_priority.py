"""Admin booking priority: which classes a regular user's run holds back on.

Exercises :func:`resolve_protected_classes` against a migrated Postgres
schema, so the join over users, gym accounts and rules, the override read
and the vacation guard run as they do in production.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.booking.priority import resolve_protected_classes
from wodbuster_worker.persistence.models import (
    BookingDayOverride,
    GymAccount,
    OperatorProfile,
    SchedulerRule,
    VacationWindow,
)
from wodbuster_worker.scheduler.rule_jobs import _protected_classes_for

# A Wednesday far enough ahead that an open vacation window covering it
# is still "open" (its end date is after now).
_DAY = date(2030, 1, 16)
_GYM = "antworktrainingcenter"


@pytest.fixture(autouse=True)
def _pin_utc_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")


@pytest.fixture
def session(postgres_engine: Engine) -> Any:
    factory = sessionmaker(bind=postgres_engine, expire_on_commit=False)
    with factory() as s:
        yield s


def _user(session: Session, *, admin: bool, status: str = "active") -> OperatorProfile:
    user = OperatorProfile(display_name="u", is_admin=admin, status=status)
    session.add(user)
    session.flush()
    return user


def _gym_account(
    session: Session, user: OperatorProfile, *, slug: str = _GYM, active: bool = True
) -> GymAccount:
    account = GymAccount(
        user_id=user.id, gym_slug=slug, display_name="g", idu=f"idu{user.id}", active=active
    )
    session.add(account)
    session.flush()
    return account


def _rule(
    session: Session,
    account: GymAccount,
    *,
    class_type: str = "WOD",
    class_time: str = "21:30",
    day_of_week: int = _DAY.weekday(),
    second_shot: tuple[str, str] | None = None,
    active: bool = True,
) -> SchedulerRule:
    rule = SchedulerRule(
        gym_account_id=account.id,
        day_of_week=day_of_week,
        class_type=class_type,
        class_time=class_time,
        booking_opens_days_before=2,
        booking_opens_at="21:30",
        second_shot_class_type=second_shot[0] if second_shot else None,
        second_shot_class_time=second_shot[1] if second_shot else None,
        active=active,
    )
    session.add(rule)
    session.flush()
    return rule


@pytest.fixture
def regular(session: Session) -> GymAccount:
    """The account whose run is being resolved."""
    return _gym_account(session, _user(session, admin=False))


@pytest.fixture
def admin_account(session: Session) -> GymAccount:
    return _gym_account(session, _user(session, admin=True))


Resolve = Callable[[GymAccount], frozenset[tuple[str, str]]]


@pytest.fixture
def resolve(session: Session) -> Resolve:
    def _resolve(account: GymAccount) -> frozenset[tuple[str, str]]:
        return resolve_protected_classes(session, gym_account=account, target_date=_DAY)

    return _resolve


def test_admin_primary_and_second_shot_are_protected(
    session: Session, regular: GymAccount, admin_account: GymAccount, resolve: Resolve
) -> None:
    _rule(session, admin_account, second_shot=("Halterofilia", "20:30"))

    assert resolve(regular) == {("wod", "21:30"), ("halterofilia", "20:30")}


def test_nothing_is_protected_without_an_admin_rule(
    session: Session, regular: GymAccount, resolve: Resolve
) -> None:
    """INV-002 upstream: a regular user competing only with regular users."""
    other = _gym_account(session, _user(session, admin=False))
    _rule(session, other)

    assert resolve(regular) == frozenset()


def test_an_admin_run_is_never_protected_against(
    session: Session, admin_account: GymAccount, resolve: Resolve
) -> None:
    """INV-004: admins are never held back, not even by another admin."""
    other_admin = _gym_account(session, _user(session, admin=True))
    _rule(session, other_admin)

    assert resolve(admin_account) == frozenset()


def test_an_admin_at_another_gym_protects_nothing(
    session: Session, regular: GymAccount, resolve: Resolve
) -> None:
    """INV-005: same class name, different gym."""
    elsewhere = _gym_account(session, _user(session, admin=True), slug="othergym")
    _rule(session, elsewhere)

    assert resolve(regular) == frozenset()


def test_admin_rules_that_do_not_run_that_day_protect_nothing(
    session: Session, regular: GymAccount, admin_account: GymAccount, resolve: Resolve
) -> None:
    _rule(session, admin_account, day_of_week=(_DAY.weekday() + 1) % 7)
    _rule(session, admin_account, class_time="07:00", active=False)
    inactive_account = _gym_account(session, _user(session, admin=True), active=False)
    _rule(session, inactive_account, class_time="08:00")
    pending_admin = _gym_account(session, _user(session, admin=True, status="pending"))
    _rule(session, pending_admin, class_time="09:00")

    assert resolve(regular) == frozenset()


def test_admin_skipping_the_day_protects_nothing(
    session: Session, regular: GymAccount, admin_account: GymAccount, resolve: Resolve
) -> None:
    """FR-003: an admin who will not book that day holds nobody back."""
    rule = _rule(session, admin_account)
    session.add(
        BookingDayOverride(
            rule_id=rule.id, gym_account_id=admin_account.id, target_date=_DAY, skip_day=True
        )
    )
    session.flush()

    assert resolve(regular) == frozenset()


def test_admin_override_protects_the_class_the_admin_will_contest(
    session: Session, regular: GymAccount, admin_account: GymAccount, resolve: Resolve
) -> None:
    """FR-002: the override target joins the admin's rule classes, and a
    suppressed second shot is not contested, so it is not protected."""
    rule = _rule(session, admin_account, second_shot=("Halterofilia", "20:30"))
    session.add(
        BookingDayOverride(
            rule_id=rule.id,
            gym_account_id=admin_account.id,
            target_date=_DAY,
            class_time="19:00",
            suppress_second_shot=True,
        )
    )
    session.flush()

    assert resolve(regular) == {("wod", "19:00"), ("wod", "21:30")}


def test_admin_on_vacation_protects_nothing(
    session: Session, regular: GymAccount, admin_account: GymAccount, resolve: Resolve
) -> None:
    """FR-003: the vacation guard the admin's own run would hit."""
    _rule(session, admin_account)
    start = datetime(_DAY.year, _DAY.month, _DAY.day, tzinfo=UTC)
    session.add(
        VacationWindow(
            gym_account_id=admin_account.id,
            start_date=start,
            end_date=start + timedelta(days=1) - timedelta(microseconds=1),
        )
    )
    session.flush()

    assert resolve(regular) == frozenset()


def test_a_failing_lookup_books_unprotected_and_keeps_the_session_usable(
    session: Session,
    regular: GymAccount,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """INV-006: a query error rolls back alone, so the job's session still
    commits and the run books without a hold."""

    def _broken(s: Session, **_kw: Any) -> frozenset[tuple[str, str]]:
        s.execute(text("SELECT * FROM no_such_table"))
        raise AssertionError("unreachable")

    monkeypatch.setattr("wodbuster_worker.scheduler.rule_jobs.resolve_protected_classes", _broken)

    protected = _protected_classes_for(session, rule_id=1, gym_account=regular, target_date=_DAY)

    assert protected == frozenset()
    assert session.execute(text("SELECT 1")).scalar_one() == 1
    session.commit()
