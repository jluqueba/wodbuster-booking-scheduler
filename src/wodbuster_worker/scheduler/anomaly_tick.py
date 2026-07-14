"""One anomaly-detector tick (US2.4, FR-026).

Scheduler wraps this in an :class:`IntervalTrigger` running every 60
seconds. Manual invocation from tests and the REPL is also supported.

Contract:

- Opens one session, detects missed windows across all active rules,
  and emits the alerts + outbox rows in the same transaction so the
  plan's "durable before dispatch" rule holds.
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
) -> list[int]:
    """Detect missed booking windows and emit anomaly alerts.

    Returns the alert ids touched during the tick — the empty list
    means "everything on schedule".
    """
    _now = now or datetime.now(tz=UTC)
    with session_factory() as session:
        missed = detect_missed_windows(
            session,
            now=_now,
            grace_period=grace_period,
            lookback=lookback,
        )
        if not missed:
            return []
        touched = emit_anomaly_alerts(session, missed, now=_now)
        session.commit()

    _log.warning(
        "anomaly.tick.missed_windows",
        missed_count=len(missed),
        alerts_touched=len(touched),
    )
    return touched


__all__ = ["run_anomaly_tick"]
