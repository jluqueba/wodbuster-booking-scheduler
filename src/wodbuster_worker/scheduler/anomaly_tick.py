"""One anomaly-detector tick (US2.4, FR-026).

Scheduler wraps this in an :class:`IntervalTrigger` running every 60
seconds. Manual invocation from tests and the REPL is also supported.

Contract:

- Opens one session, detects missed windows across all active rules,
  and emits the alerts + outbox rows in the same transaction so the
  plan's "durable before dispatch" rule holds.
- Re-notification is rate-limited by the detector's re-fire interval,
  so a missed window that stays detectable for the whole lookback
  produces one round of notifications, not one per tick.
- Closes open anomaly alerts that resolved or aged out, on every tick.
- Exceptions bubble up to the scheduler wrapper below, which
  swallows them after logging so a bad tick cannot take the process
  down. Callers (tests, REPL) get the exception raw.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import structlog

from ..heartbeat.anomaly import (
    DEFAULT_GRACE_PERIOD,
    DEFAULT_LOOKBACK,
    DEFAULT_REFIRE_INTERVAL,
    DEFAULT_RETENTION,
    close_resolved_anomalies,
    detect_missed_windows,
    emit_anomaly_alerts,
)
from .heartbeat_tick import SessionFactory

_log = structlog.get_logger(__name__)


def run_anomaly_tick(
    session_factory: SessionFactory,
    *,
    now: datetime | None = None,
    grace_period: timedelta = DEFAULT_GRACE_PERIOD,
    lookback: timedelta = DEFAULT_LOOKBACK,
    refire_interval: timedelta = DEFAULT_REFIRE_INTERVAL,
    retention: timedelta = DEFAULT_RETENTION,
) -> list[int]:
    """Detect missed booking windows and emit anomaly alerts.

    Returns the alert ids notified during the tick — the empty list
    means "nothing new to tell the operator", which covers both
    "everything on schedule" and "the open alert is still the same
    one we already reported".

    Closing runs on every tick, including the ones with nothing to
    detect: an alert that stopped being true has to clear the
    dashboard banner on its own.
    """
    _now = now or datetime.now(tz=UTC)
    with session_factory() as session:
        missed = detect_missed_windows(
            session,
            now=_now,
            grace_period=grace_period,
            lookback=lookback,
        )
        touched = (
            emit_anomaly_alerts(session, missed, now=_now, refire_interval=refire_interval)
            if missed
            else []
        )
        # After emission, so a window that is still missing cannot be
        # closed and re-opened inside the same tick.
        closed = close_resolved_anomalies(session, now=_now, retention=retention)
        # Committed unconditionally. The pass above also prunes resolved
        # windows off alerts it leaves open, which is a write that no
        # return value reports, and an "only commit when something
        # happened" guard silently discarded it.
        session.commit()

    if missed:
        _log.warning(
            "anomaly.tick.missed_windows",
            missed_count=len(missed),
            alerts_notified=len(touched),
        )
    if closed:
        _log.info("anomaly.tick.alerts_closed", closed_count=len(closed))
    return touched


__all__ = ["run_anomaly_tick"]
