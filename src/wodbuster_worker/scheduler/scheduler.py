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
- **In-memory jobstore for the heartbeat.** Jobs are recreated on
  every startup so we do not need job-run history to survive
  restarts. Slice for US-001 (booking core) will introduce a
  ``SQLAlchemyJobStore`` for date-triggered booking jobs whose loss
  across restarts would be a correctness bug; the heartbeat has no
  such requirement.
- **Run once immediately on startup.** Fresh deployments should not
  wait an hour to observe the cookie state. ``next_run_time=now`` on
  the trigger fires the first tick right after startup.
- **``max_instances=1`` + ``coalesce=True``.** If a tick overruns its
  interval (unlikely — the probe is a single HTTP call plus a small
  transaction) the scheduler drops the queued extra rather than
  stacking concurrent runs.
"""

from __future__ import annotations

from datetime import UTC, datetime

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger

from ..heartbeat.probe import HeartbeatProbe
from .heartbeat_tick import SessionFactory, run_heartbeat_tick

_log = structlog.get_logger(__name__)

HEARTBEAT_JOB_ID = "cookie_heartbeat"


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


__all__ = [
    "HEARTBEAT_JOB_ID",
    "build_scheduler",
    "register_heartbeat_job",
]
