"""Per-rule scheduling primitives (US1.9, US1.10 support).

Shared arithmetic for turning a :class:`SchedulerRule` into concrete
booking-window datetimes plus APScheduler wiring helpers to register
and refresh a booking job per rule.

The scheduler owns one job per active rule (``BOOKING_JOB_ID_PREFIX +
str(rule.id)``). Each job is a ``DateTrigger`` at the rule's next
booking-window open time. When the job fires the callable
(:func:`book_rule`) resolves the rule from the DB, invokes the
:class:`BookingExecutor`, and re-registers itself for the following
week's window.

Persistence choice: in-memory jobstore. Restart is handled by the
bootstrap step (:func:`register_rule_bootstrap_job` in
``scheduler/scheduler.py``) which re-derives the schedule from every
active rule on startup. A durable :class:`SQLAlchemyJobStore` is not
necessary for correctness — the source of truth is the ``scheduler_rule``
table — and adds a live coupling between APScheduler's schema and our
migration story. If we ever need "resume the exact tick after a
crash inside the job" we revisit.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import structlog
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.date import DateTrigger

from ..booking.executor import BookingExecutor
from ..persistence.models import SchedulerRule

_log = structlog.get_logger(__name__)

BOOKING_JOB_ID_PREFIX = "booking_rule_"


SessionFactory = Callable[[], AbstractContextManager[Any]]


# ---------------------------------------------------------------------------
# Time arithmetic
# ---------------------------------------------------------------------------


def _operator_timezone() -> ZoneInfo:
    """Return the timezone in which every rule's ``HH:MM`` is interpreted.

    Reads ``WORKER_TIMEZONE`` from the environment (default
    ``Europe/Madrid``). Kept as a lazy lookup so tests can override
    via ``monkeypatch.setenv``. The gym runs on the operator's local
    clock; treating ``HH:MM`` as UTC (as an earlier draft did) fires
    the scheduler at the wrong instant.
    """
    return ZoneInfo(os.environ.get("WORKER_TIMEZONE", "Europe/Madrid"))


def next_window_open_for_rule(rule: SchedulerRule, now: datetime) -> datetime:
    """Return the next booking-window open instant for ``rule``.

    Trigger day is ``(day_of_week - booking_opens_days_before) mod 7``;
    the window opens on that weekday at ``booking_opens_at``
    interpreted in the operator's timezone (see
    :func:`_operator_timezone`). If today matches and the time is
    still in the future, the same-day instant is returned. Otherwise
    the function rolls forward one week. Returned datetime is UTC.
    """
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    opens_at = _parse_hhmm(rule.booking_opens_at)
    trigger_day = (rule.day_of_week - rule.booking_opens_days_before) % 7
    return _next_occurrence(now=now, day_of_week=trigger_day, at=opens_at)


def target_slot_for_window(rule: SchedulerRule, window_open: datetime) -> datetime:
    """Return the class-start datetime paired with ``window_open``.

    Class start day is ``booking_opens_days_before`` days after the
    window opens; the clock time is ``class_time`` in the operator's
    timezone. Returned datetime is UTC.
    """
    if window_open.tzinfo is None:
        raise ValueError("window_open must be timezone-aware")
    class_time = _parse_hhmm(rule.class_time)
    tz = _operator_timezone()
    # Move to the operator's local zone so day arithmetic uses local
    # midnight (avoids off-by-one when the UTC window and local day
    # straddle midnight).
    local_window = window_open.astimezone(tz)
    class_day_local = local_window + timedelta(days=rule.booking_opens_days_before)
    local_start = class_day_local.replace(
        hour=class_time.hour, minute=class_time.minute, second=0, microsecond=0
    )
    return local_start.astimezone(UTC)


def _parse_hhmm(value: str) -> time:
    hh, mm = value.split(":")
    return time(hour=int(hh), minute=int(mm))


def _next_occurrence(*, now: datetime, day_of_week: int, at: time) -> datetime:
    """Next ``day_of_week`` at ``at`` (in the operator's tz), as UTC.

    Both weekday arithmetic and the ``at`` clock time are anchored
    in the operator's local zone so DST transitions and non-UTC
    operators do not mis-fire.
    """
    tz = _operator_timezone()
    local_now = now.astimezone(tz)
    local_today = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    days_ahead = (day_of_week - local_now.weekday()) % 7
    local_candidate = (local_today + timedelta(days=days_ahead)).replace(
        hour=at.hour, minute=at.minute
    )
    candidate = local_candidate.astimezone(UTC)
    if candidate <= now:
        candidate = (local_candidate + timedelta(days=7)).astimezone(UTC)
    return candidate


# ---------------------------------------------------------------------------
# Job callable
# ---------------------------------------------------------------------------


def book_rule(
    rule_id: int,
    *,
    executor: BookingExecutor,
    session_factory: SessionFactory,
    scheduler: BackgroundScheduler | None = None,
) -> None:
    """Fire one booking attempt for ``rule_id`` and re-schedule.

    Runs on APScheduler's thread pool. Every branch that returns
    without exceptions is a valid completion — we log and move on so
    a single bad rule cannot block the whole scheduler.

    Rescheduling: after the executor returns (or raises), we compute
    the following week's window and register a fresh job. That keeps
    the ``next run`` state authoritative in APScheduler rather than
    forcing us to rely on a cron trigger's own drift math.
    """
    with session_factory() as session:
        rule = session.get(SchedulerRule, rule_id)
        if rule is None or not rule.active:
            _log.info(
                "scheduler.booking.rule_gone",
                rule_id=rule_id,
                reason="deleted" if rule is None else "inactive",
            )
            return

        now = datetime.now(tz=UTC)
        window_open = next_window_open_for_rule(rule, now=now - timedelta(seconds=1))
        target_slot = target_slot_for_window(rule, window_open)

        # Refresh SQLAlchemy session-tracked attributes onto local
        # variables before we close the session — the executor uses
        # its own session factory for the outcome write.
        rule_snapshot = _detach_rule(rule)

    _log.info(
        "scheduler.booking.fire",
        rule_id=rule_id,
        target_slot=target_slot.isoformat(),
    )
    try:
        executor.book(rule=rule_snapshot, target_slot=target_slot)
    except Exception:  # pragma: no cover - logged and swallowed
        _log.exception("scheduler.booking.executor_raised", rule_id=rule_id)
    finally:
        if scheduler is not None:
            _schedule_next(
                scheduler=scheduler,
                rule=rule_snapshot,
                executor=executor,
                session_factory=session_factory,
            )


def _detach_rule(rule: SchedulerRule) -> SchedulerRule:
    """Return a transient copy of ``rule`` safe to use after session close."""
    copy = SchedulerRule(
        operator_id=rule.operator_id,
        day_of_week=rule.day_of_week,
        class_type=rule.class_type,
        class_time=rule.class_time,
        booking_opens_days_before=rule.booking_opens_days_before,
        booking_opens_at=rule.booking_opens_at,
        second_shot_class_type=rule.second_shot_class_type,
        second_shot_class_time=rule.second_shot_class_time,
        active=rule.active,
    )
    copy.id = rule.id
    return copy


# ---------------------------------------------------------------------------
# Scheduler wiring
# ---------------------------------------------------------------------------


def register_rule_job(
    scheduler: BackgroundScheduler,
    rule: SchedulerRule,
    *,
    executor: BookingExecutor,
    session_factory: SessionFactory,
    now: datetime | None = None,
) -> str:
    """Register (or replace) a DateTrigger job for ``rule``.

    Returns the APScheduler job id. Idempotent: an existing job with
    the same id is removed before the new one is added.
    """
    now = now or datetime.now(tz=UTC)
    run_at = next_window_open_for_rule(rule, now=now)
    job_id = _job_id(rule.id)

    if scheduler.get_job(job_id) is not None:
        scheduler.remove_job(job_id)

    scheduler.add_job(
        func=book_rule,
        trigger=DateTrigger(run_date=run_at),
        args=[rule.id],
        kwargs={
            "executor": executor,
            "session_factory": session_factory,
            "scheduler": scheduler,
        },
        id=job_id,
        max_instances=1,
        coalesce=True,
    )
    _log.info(
        "scheduler.booking.registered",
        rule_id=rule.id,
        job_id=job_id,
        run_at=run_at.isoformat(),
    )
    return job_id


def unregister_rule_job(scheduler: BackgroundScheduler, rule_id: int) -> bool:
    """Remove the job for ``rule_id`` if present. Returns True if removed."""
    job_id = _job_id(rule_id)
    if scheduler.get_job(job_id) is None:
        return False
    scheduler.remove_job(job_id)
    _log.info("scheduler.booking.unregistered", rule_id=rule_id, job_id=job_id)
    return True


def _schedule_next(
    *,
    scheduler: BackgroundScheduler,
    rule: SchedulerRule,
    executor: BookingExecutor,
    session_factory: SessionFactory,
) -> None:
    """Re-register the rule's job for the following window."""
    try:
        register_rule_job(
            scheduler,
            rule,
            executor=executor,
            session_factory=session_factory,
            # Small offset so ``next_window_open_for_rule`` rolls
            # forward past the window that just fired.
            now=datetime.now(tz=UTC) + timedelta(seconds=1),
        )
    except Exception:  # pragma: no cover - logged and swallowed
        _log.exception("scheduler.booking.reschedule_failed", rule_id=rule.id)


def _job_id(rule_id: int) -> str:
    return f"{BOOKING_JOB_ID_PREFIX}{rule_id}"


__all__ = [
    "BOOKING_JOB_ID_PREFIX",
    "SessionFactory",
    "book_rule",
    "next_window_open_for_rule",
    "register_rule_job",
    "target_slot_for_window",
    "unregister_rule_job",
]
