"""APScheduler bootstrap for background jobs (US1.9 slice, US4.1 wiring).

Owns a single :class:`BackgroundScheduler` per FastAPI process. Jobs are
registered as pure Python callables; the scheduler itself is
thread-based so sync SQLAlchemy work inside the callable does not
compete with FastAPI's event loop.

Design choices worth calling out:

- **BackgroundScheduler over AsyncIOScheduler.** The heartbeat probe is
  a sync stack (SQLAlchemy + httpx sync). A thread-based scheduler
  matches the work; an ``AsyncIOScheduler`` would require wrapping
  every job in ``run_in_executor`` and would tangle the scheduler's
  lifecycle with FastAPI's event loop.
- **In-memory jobstore.** Jobs are recreated on every startup so we
  do not need job-run history to survive restarts. Booking rule jobs
  are rehydrated by :func:`register_rule_bootstrap_jobs` from the
  ``scheduler_rule`` table on startup, which is the source of truth.
  A :class:`SQLAlchemyJobStore` was considered and rejected: it
  would couple APScheduler's internal schema to our migration story
  without buying us anything the bootstrap step does not already.
- **Run once immediately on startup.** Fresh deployments should not
  wait an hour to observe the cookie state. ``next_run_time=now`` on
  the trigger fires the first tick right after startup.
- **``max_instances=1`` + ``coalesce=True``.** If a tick overruns its
  interval (unlikely — the probe is a single HTTP call plus a small
  transaction) the scheduler drops the queued extra rather than
  stacking concurrent runs.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..booking.executor import BookingExecutor
from ..heartbeat.probe import HeartbeatProbe
from ..notifications.dispatcher import NotificationDispatcher
from ..persistence.models import SchedulerRule
from .anomaly_tick import run_anomaly_tick
from .heartbeat_tick import SessionFactory, run_heartbeat_tick
from .rule_jobs import register_rule_job

_log = structlog.get_logger(__name__)

HEARTBEAT_JOB_ID = "cookie_heartbeat"
DISPATCHER_JOB_ID = "notification_dispatcher"
ANOMALY_JOB_ID = "anomaly_detector"


def build_scheduler() -> BackgroundScheduler:
    """Return an unstarted :class:`BackgroundScheduler`.

    Extracted from :func:`register_heartbeat_job` so tests that only
    want to check job registration can operate on a stopped scheduler.
    """
    return BackgroundScheduler(timezone="UTC")


def register_heartbeat_job(
    scheduler: BackgroundScheduler,
    probe: HeartbeatProbe,
    session_factory: SessionFactory,
    *,
    interval_hours: int = 1,
) -> None:
    """Register the hourly heartbeat tick on ``scheduler``.

    Explicitly removes any pre-existing job with the same id first so
    the function is idempotent on the in-memory jobstore
    (``add_job(..., replace_existing=True)`` only replaces on
    persistent stores). The single-replica deployment means we do not
    otherwise need cross-process coordination.
    """

    def _tick() -> None:
        run_heartbeat_tick(probe, session_factory)

    if scheduler.get_job(HEARTBEAT_JOB_ID) is not None:
        scheduler.remove_job(HEARTBEAT_JOB_ID)

    scheduler.add_job(
        func=_tick,
        trigger=IntervalTrigger(hours=interval_hours),
        id=HEARTBEAT_JOB_ID,
        max_instances=1,
        coalesce=True,
        # Force an immediate first run so the operator sees a probe
        # outcome without waiting the full interval.
        next_run_time=datetime.now(tz=UTC),
    )
    _log.info(
        "scheduler.heartbeat_job_registered",
        job_id=HEARTBEAT_JOB_ID,
        interval_hours=interval_hours,
    )


def register_dispatcher_job(
    scheduler: BackgroundScheduler,
    dispatcher: NotificationDispatcher,
    *,
    interval_seconds: int = 5,
) -> None:
    """Register the notification-outbox dispatcher on ``scheduler``.

    Same idempotency contract as :func:`register_heartbeat_job`. The
    dispatcher owns its own session factory internally, so this
    wrapper only needs the interval knob and the dispatcher instance.
    """

    def _tick() -> None:
        dispatcher.tick()

    if scheduler.get_job(DISPATCHER_JOB_ID) is not None:
        scheduler.remove_job(DISPATCHER_JOB_ID)

    scheduler.add_job(
        func=_tick,
        trigger=IntervalTrigger(seconds=interval_seconds),
        id=DISPATCHER_JOB_ID,
        max_instances=1,
        coalesce=True,
        # Fire immediately so a pending row queued on startup does
        # not wait a full interval to leave.
        next_run_time=datetime.now(tz=UTC),
    )
    _log.info(
        "scheduler.dispatcher_job_registered",
        job_id=DISPATCHER_JOB_ID,
        interval_seconds=interval_seconds,
    )


def register_anomaly_job(
    scheduler: BackgroundScheduler,
    session_factory: SessionFactory,
    *,
    interval_seconds: int = 60,
) -> None:
    """Register the per-run anomaly detector on ``scheduler``.

    Ticks every ``interval_seconds`` (default 60). Same idempotency
    contract as the other registrations: removes any pre-existing
    job with the same id before adding.
    """

    def _tick() -> None:
        try:
            run_anomaly_tick(session_factory)
        except Exception:
            # APScheduler swallows exceptions inside jobs; log them
            # explicitly so the operator sees the failure in the app
            # logs rather than only in APScheduler's stderr fallback.
            _log.exception("scheduler.anomaly_tick_failed")

    if scheduler.get_job(ANOMALY_JOB_ID) is not None:
        scheduler.remove_job(ANOMALY_JOB_ID)

    scheduler.add_job(
        func=_tick,
        trigger=IntervalTrigger(seconds=interval_seconds),
        id=ANOMALY_JOB_ID,
        max_instances=1,
        coalesce=True,
        # Delay the first run by one interval so the scheduler has a
        # chance to publish outcomes before we start looking for
        # missing ones. Immediate fire would race the first booking
        # tick on cold start.
        next_run_time=datetime.now(tz=UTC) + timedelta(seconds=interval_seconds),
    )
    _log.info(
        "scheduler.anomaly_job_registered",
        job_id=ANOMALY_JOB_ID,
        interval_seconds=interval_seconds,
    )


def register_rule_bootstrap_jobs(
    scheduler: BackgroundScheduler,
    *,
    executor: BookingExecutor,
    session_factory: SessionFactory,
) -> int:
    """Register a booking-window job for every active scheduler rule.

    Called once on app startup. Iterates every active rule, computes
    the next window open time, and registers a DateTrigger job. Rules
    added later go through :func:`rule_jobs.register_rule_job` from
    the mutation hook in :mod:`rules.routes` (US1.10).

    Returns the number of jobs registered so the caller can log a
    coarse count.
    """
    from sqlalchemy import select  # local import: rare hot path.

    registered = 0
    with session_factory() as session:
        rules = (
            session.execute(select(SchedulerRule).where(SchedulerRule.active.is_(True)))
            .scalars()
            .all()
        )
        for rule in rules:
            try:
                register_rule_job(
                    scheduler,
                    rule,
                    executor=executor,
                    session_factory=session_factory,
                )
            except ValueError as exc:
                _log.warning(
                    "scheduler.booking.bootstrap_skip",
                    rule_id=rule.id,
                    error=str(exc),
                )
                continue
            registered += 1
    _log.info(
        "scheduler.booking.bootstrap_done",
        rules_registered=registered,
    )
    return registered


__all__ = [
    "ANOMALY_JOB_ID",
    "DISPATCHER_JOB_ID",
    "HEARTBEAT_JOB_ID",
    "build_scheduler",
    "register_anomaly_job",
    "register_dispatcher_job",
    "register_heartbeat_job",
    "register_rule_bootstrap_jobs",
]
