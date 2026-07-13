"""Unit tests for per-rule scheduler wiring (US1.9, US1.10).

Covers the pure arithmetic (:func:`next_window_open_for_rule`,
:func:`target_slot_for_window`) and the scheduler-side helpers
(:func:`register_rule_job`, :func:`unregister_rule_job`,
:func:`book_rule`).

Scheduler tests drive a real :class:`BackgroundScheduler` in stopped
state — we assert on ``get_job`` state without actually running any
jobs. The end-to-end "job fires → executor invoked" path is covered
by :func:`book_rule` tests with an injected fake session factory.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from wodbuster_worker.persistence.models import SchedulerRule
from wodbuster_worker.scheduler.rule_jobs import (
    BOOKING_JOB_ID_PREFIX,
    book_rule,
    next_window_open_for_rule,
    register_rule_job,
    target_slot_for_window,
    unregister_rule_job,
)


@pytest.fixture(autouse=True)
def _pin_utc_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``WORKER_TIMEZONE=UTC`` for the arithmetic tests.

    Real deployments read ``WORKER_TIMEZONE`` (default
    ``Europe/Madrid``) and interpret every rule's ``HH:MM`` in that
    zone. Pinning to UTC here keeps the numeric assertions readable —
    a dedicated ``test_operator_timezone_is_honored_*`` case exercises
    the real Madrid path.
    """
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")


def _rule(
    rule_id: int = 42,
    *,
    day_of_week: int = 2,  # Wednesday
    class_time: str = "21:30",
    booking_opens_days_before: int = 2,
    booking_opens_at: str = "21:30",
) -> SchedulerRule:
    rule = SchedulerRule(
        operator_id=1,
        day_of_week=day_of_week,
        class_type="WOD",
        class_time=class_time,
        booking_opens_days_before=booking_opens_days_before,
        booking_opens_at=booking_opens_at,
        active=True,
    )
    rule.id = rule_id
    return rule


# ---------------------------------------------------------------------------
# next_window_open_for_rule
# ---------------------------------------------------------------------------


def test_next_window_open_is_trigger_day_at_opens_at() -> None:
    # 2026-07-13 is a Monday. Rule: attend Wed (2), opens 2 days before at 21:30
    # → trigger day = (2 - 2) % 7 = 0 = Monday.
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    rule = _rule(day_of_week=2, booking_opens_days_before=2, booking_opens_at="21:30")

    result = next_window_open_for_rule(rule, now=now)

    assert result == datetime(2026, 7, 13, 21, 30, tzinfo=UTC)


def test_next_window_open_rolls_forward_when_past() -> None:
    # Monday 22:00, but the window should have opened at 21:30 today.
    # Roll forward one week to next Monday.
    now = datetime(2026, 7, 13, 22, 0, tzinfo=UTC)
    rule = _rule(day_of_week=2, booking_opens_days_before=2, booking_opens_at="21:30")

    result = next_window_open_for_rule(rule, now=now)

    assert result == datetime(2026, 7, 20, 21, 30, tzinfo=UTC)


def test_next_window_open_naive_datetime_raises() -> None:
    rule = _rule()
    with pytest.raises(ValueError, match="timezone-aware"):
        next_window_open_for_rule(rule, now=datetime(2026, 7, 13, 12, 0))


def test_next_window_open_wraps_across_week_boundary() -> None:
    # Attend Monday (0), opens 3 days before at 22:40
    # → trigger day = (0 - 3) % 7 = 4 = Friday.
    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)  # Wed
    rule = _rule(day_of_week=0, booking_opens_days_before=3, booking_opens_at="22:40")

    result = next_window_open_for_rule(rule, now=now)

    assert result == datetime(2026, 7, 10, 22, 40, tzinfo=UTC)


def test_operator_timezone_is_honored_for_next_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # July 13 2026 is a Monday. CEST = UTC+2, so 22:40 Madrid on Mon
    # July 13 is 20:40 UTC. Verifies the operator's HH:MM is
    # interpreted in the configured zone rather than UTC.
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    rule = _rule(day_of_week=0, booking_opens_days_before=0, booking_opens_at="22:40")

    result = next_window_open_for_rule(rule, now=now)

    assert result == datetime(2026, 7, 13, 20, 40, tzinfo=UTC)


# ---------------------------------------------------------------------------
# target_slot_for_window
# ---------------------------------------------------------------------------


def test_target_slot_is_days_ahead_at_class_time() -> None:
    window_open = datetime(2026, 7, 13, 21, 30, tzinfo=UTC)  # Monday
    rule = _rule(class_time="21:30", booking_opens_days_before=2)

    result = target_slot_for_window(rule, window_open)

    # Monday + 2 days = Wednesday, class starts at 21:30.
    assert result == datetime(2026, 7, 15, 21, 30, tzinfo=UTC)


def test_target_slot_handles_different_class_time_than_window() -> None:
    window_open = datetime(2026, 7, 13, 22, 40, tzinfo=UTC)
    rule = _rule(class_time="07:30", booking_opens_days_before=3)

    result = target_slot_for_window(rule, window_open)

    # Monday + 3 days = Thursday, class starts at 07:30.
    assert result == datetime(2026, 7, 16, 7, 30, tzinfo=UTC)


def test_target_slot_naive_raises() -> None:
    rule = _rule()
    with pytest.raises(ValueError, match="timezone-aware"):
        target_slot_for_window(rule, datetime(2026, 7, 13, 21, 30))


def test_target_slot_uses_operator_timezone_for_class_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Window opens Fri 22:40 Madrid (= 20:40 UTC). Class time is
    # 07:30 Madrid on the following Monday. Verifies that both the
    # day arithmetic and the HH:MM hop through the operator zone.
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")
    window_open = datetime(2026, 7, 10, 20, 40, tzinfo=UTC)  # Fri 22:40 Madrid
    rule = _rule(class_time="07:30", booking_opens_days_before=3)

    result = target_slot_for_window(rule, window_open)

    # Mon 07:30 Madrid == Mon 05:30 UTC (CEST).
    assert result == datetime(2026, 7, 13, 5, 30, tzinfo=UTC)


# ---------------------------------------------------------------------------
# register_rule_job / unregister_rule_job
# ---------------------------------------------------------------------------


def _fresh_scheduler() -> BackgroundScheduler:
    return BackgroundScheduler(timezone="UTC")


def _null_session_factory() -> Any:  # pragma: no cover - never called here
    raise AssertionError("session factory should not be invoked in stopped scheduler")


def test_register_rule_job_creates_date_trigger_at_window_open() -> None:
    scheduler = _fresh_scheduler()
    executor = MagicMock()
    rule = _rule(day_of_week=2, booking_opens_days_before=2, booking_opens_at="21:30")
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    job_id = register_rule_job(
        scheduler,
        rule,
        executor=executor,
        session_factory=_null_session_factory,
        now=now,
    )

    assert job_id == f"{BOOKING_JOB_ID_PREFIX}42"
    job = scheduler.get_job(job_id)
    assert job is not None
    assert isinstance(job.trigger, DateTrigger)
    assert job.trigger.run_date == datetime(2026, 7, 13, 21, 30, tzinfo=UTC)
    assert job.max_instances == 1
    assert job.coalesce is True


def test_register_rule_job_replaces_existing() -> None:
    scheduler = _fresh_scheduler()
    executor = MagicMock()
    rule = _rule(day_of_week=2)
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)

    register_rule_job(
        scheduler,
        rule,
        executor=executor,
        session_factory=_null_session_factory,
        now=now,
    )
    # Re-register with a shifted "now" — job should carry the fresh
    # trigger, not stack a duplicate.
    later = datetime(2026, 7, 13, 22, 0, tzinfo=UTC)
    register_rule_job(
        scheduler,
        rule,
        executor=executor,
        session_factory=_null_session_factory,
        now=later,
    )

    jobs = [j for j in scheduler.get_jobs() if j.id.startswith(BOOKING_JOB_ID_PREFIX)]
    assert len(jobs) == 1
    # After 22:00 the window has passed today — next window is next
    # Monday at 21:30.
    assert jobs[0].trigger.run_date == datetime(2026, 7, 20, 21, 30, tzinfo=UTC)


def test_unregister_rule_job_removes_existing() -> None:
    scheduler = _fresh_scheduler()
    executor = MagicMock()
    rule = _rule(rule_id=99)
    now = datetime(2026, 7, 13, 12, 0, tzinfo=UTC)
    register_rule_job(
        scheduler,
        rule,
        executor=executor,
        session_factory=_null_session_factory,
        now=now,
    )

    removed = unregister_rule_job(scheduler, 99)
    assert removed is True
    assert scheduler.get_job(f"{BOOKING_JOB_ID_PREFIX}99") is None


def test_unregister_rule_job_missing_returns_false() -> None:
    scheduler = _fresh_scheduler()
    assert unregister_rule_job(scheduler, 12345) is False


def test_register_rule_job_malformed_hhmm_raises() -> None:
    scheduler = _fresh_scheduler()
    executor = MagicMock()
    rule = _rule()
    rule.booking_opens_at = "not-a-time"
    with pytest.raises(ValueError):
        register_rule_job(
            scheduler,
            rule,
            executor=executor,
            session_factory=_null_session_factory,
            now=datetime(2026, 7, 13, 12, 0, tzinfo=UTC),
        )


# ---------------------------------------------------------------------------
# book_rule (job callable)
# ---------------------------------------------------------------------------


def _session_factory_returning(rule: SchedulerRule | None) -> Any:
    """Return a context-manager factory whose session.get(SchedulerRule, ...)
    yields ``rule``. Any other .get lookup returns MagicMock."""

    @contextmanager
    def factory() -> Iterator[Any]:
        session = MagicMock()

        def _get(model: Any, key: Any) -> Any:
            if model is SchedulerRule and rule is not None and key == rule.id:
                return rule
            return None

        session.get.side_effect = _get
        yield session

    return factory


def test_book_rule_fires_executor_with_derived_target_slot() -> None:
    rule = _rule()
    factory = _session_factory_returning(rule)
    executor = MagicMock()

    book_rule(
        rule.id,
        executor=executor,
        session_factory=factory,
        scheduler=None,  # skip reschedule to keep this test focused
    )

    assert executor.book.call_count == 1
    call = executor.book.call_args
    assert call.kwargs["rule"].id == rule.id
    target_slot = call.kwargs["target_slot"]
    # target_slot is a Wednesday (rule.day_of_week=2) at 21:30 in the
    # future.
    assert target_slot.weekday() == 2
    assert target_slot.hour == 21
    assert target_slot.minute == 30
    assert target_slot > datetime.now(tz=UTC)


def test_book_rule_skips_when_rule_deleted() -> None:
    factory = _session_factory_returning(None)
    executor = MagicMock()

    book_rule(999, executor=executor, session_factory=factory, scheduler=None)

    executor.book.assert_not_called()


def test_book_rule_skips_when_rule_inactive() -> None:
    rule = _rule()
    rule.active = False
    factory = _session_factory_returning(rule)
    executor = MagicMock()

    book_rule(rule.id, executor=executor, session_factory=factory, scheduler=None)

    executor.book.assert_not_called()


def test_book_rule_reschedules_when_scheduler_provided() -> None:
    rule = _rule()
    factory = _session_factory_returning(rule)
    executor = MagicMock()
    scheduler = _fresh_scheduler()

    book_rule(rule.id, executor=executor, session_factory=factory, scheduler=scheduler)

    job = scheduler.get_job(f"{BOOKING_JOB_ID_PREFIX}{rule.id}")
    assert job is not None
    # Rescheduled to some point in the future (the next matching
    # window occurrence; exact instant depends on the wall clock at
    # test-run time so we only assert "future").
    assert job.trigger.run_date > datetime.now(tz=UTC)


def test_book_rule_swallows_executor_exception_and_still_reschedules() -> None:
    rule = _rule()
    factory = _session_factory_returning(rule)
    executor = MagicMock()
    executor.book.side_effect = RuntimeError("boom")
    scheduler = _fresh_scheduler()

    # Must not raise even when the executor blows up.
    book_rule(rule.id, executor=executor, session_factory=factory, scheduler=scheduler)

    # Reschedule still happened.
    job = scheduler.get_job(f"{BOOKING_JOB_ID_PREFIX}{rule.id}")
    assert job is not None
