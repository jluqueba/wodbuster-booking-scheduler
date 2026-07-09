"""Unit tests for scheduler bootstrap (US4.1 wiring).

Exercises the ``build_scheduler`` + ``register_heartbeat_job`` shape
without starting the scheduler thread. We trust APScheduler itself to
run jobs; the point here is that our registration hands it a
well-shaped job (id, trigger type, coalesce / max_instances) so a
future refactor cannot silently break the scheduling contract.
"""

from __future__ import annotations

from datetime import timedelta

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from wodbuster_worker.heartbeat.probe import HeartbeatProbe
from wodbuster_worker.notifications.dispatcher import NotificationDispatcher
from wodbuster_worker.scheduler.scheduler import (
    DISPATCHER_JOB_ID,
    HEARTBEAT_JOB_ID,
    build_scheduler,
    register_dispatcher_job,
    register_heartbeat_job,
)


class _NullProbe:
    """Duck-typed :class:`HeartbeatProbe` — the scheduler never invokes it here."""

    def run(self, session, operator_id):  # pragma: no cover - unused
        raise AssertionError("probe should not run in a stopped scheduler")


def _null_session_factory():  # pragma: no cover - unused
    raise AssertionError("session factory should not be called in a stopped scheduler")


def test_build_scheduler_returns_utc_background_scheduler() -> None:
    scheduler = build_scheduler()

    assert isinstance(scheduler, BackgroundScheduler)
    # Timezone is UTC so scheduled datetimes never depend on the
    # container's local zone (which is UTC in Container Apps but
    # local elsewhere).
    assert str(scheduler.timezone) == "UTC"
    # Not started yet — pass to register_heartbeat_job first.
    assert not scheduler.running


def test_register_heartbeat_job_adds_expected_job() -> None:
    scheduler = build_scheduler()
    probe: HeartbeatProbe = _NullProbe()  # type: ignore[assignment]

    register_heartbeat_job(scheduler, probe, _null_session_factory)

    job = scheduler.get_job(HEARTBEAT_JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    # Default cadence: 1 hour.
    assert job.trigger.interval == timedelta(hours=1)
    # Coalesce + max_instances guard against overrunning ticks
    # stacking up when the app is under load or a probe stalls.
    assert job.coalesce is True
    assert job.max_instances == 1
    # First run scheduled immediately so a fresh deploy does not wait.
    assert job.next_run_time is not None


def test_register_heartbeat_job_is_idempotent_with_custom_interval() -> None:
    scheduler = build_scheduler()
    probe: HeartbeatProbe = _NullProbe()  # type: ignore[assignment]

    register_heartbeat_job(scheduler, probe, _null_session_factory, interval_hours=1)
    # Re-register with a different cadence: replace_existing=True in
    # the implementation means the second call must overwrite the
    # first, not error.
    register_heartbeat_job(scheduler, probe, _null_session_factory, interval_hours=4)

    job = scheduler.get_job(HEARTBEAT_JOB_ID)
    assert job is not None
    assert job.trigger.interval == timedelta(hours=4)


def test_register_dispatcher_job_adds_expected_job() -> None:
    scheduler = build_scheduler()
    dispatcher = NotificationDispatcher(
        bot_token=None, session_factory=_null_session_factory
    )

    register_dispatcher_job(scheduler, dispatcher)

    job = scheduler.get_job(DISPATCHER_JOB_ID)
    assert job is not None
    assert isinstance(job.trigger, IntervalTrigger)
    # Default cadence: 5 seconds.
    assert job.trigger.interval == timedelta(seconds=5)
    assert job.coalesce is True
    assert job.max_instances == 1
    # First run scheduled immediately so a startup-time pending row
    # does not wait a full interval to leave.
    assert job.next_run_time is not None


def test_register_dispatcher_job_is_idempotent() -> None:
    scheduler = build_scheduler()
    dispatcher = NotificationDispatcher(
        bot_token=None, session_factory=_null_session_factory
    )

    register_dispatcher_job(scheduler, dispatcher, interval_seconds=5)
    # Re-register with a different cadence: must overwrite, not error.
    register_dispatcher_job(scheduler, dispatcher, interval_seconds=15)

    job = scheduler.get_job(DISPATCHER_JOB_ID)
    assert job is not None
    assert job.trigger.interval == timedelta(seconds=15)
