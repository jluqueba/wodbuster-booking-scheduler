"""One heartbeat tick across all operators (US4.1).

The scheduler wraps this function in an :class:`IntervalTrigger` so it
runs hourly. Manual invocation (from a debug route, a REPL, or a
component test) is also supported — the tick is a plain callable with
no scheduler dependency.

Contract:

- Iterates over every operator profile in the database. Single-tenant
  today; the design still enumerates so we do not have to refactor
  when a second operator lands.
- Skips operators with no cookie on file (``NoCookieOnFile``) without
  logging an error — that is a normal state, not a failure.
- Each operator's probe runs in its own transaction so one operator's
  transient failure never rolls back another operator's probe.
- Exceptions from the probe are caught and logged so a single tick can
  never take the scheduler down. APScheduler already suppresses
  exceptions inside jobs, but explicit logging here means the
  operator sees the failure in the app logs rather than only in
  APScheduler's stderr fallback.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from contextlib import AbstractContextManager

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..heartbeat.alerts import (
    apply_alert_action,
    evaluate_cookie_expiring,
    previous_heartbeat_at,
)
from ..heartbeat.probe import HeartbeatOutcome, HeartbeatProbe, NoCookieOnFile
from ..persistence.models import OperatorProfile

_log = structlog.get_logger(__name__)

# Type aliases for the two injectables. The session factory follows
# ``persistence.engine.get_session``'s shape (context-manager yielding
# a Session that commits on success / rolls back on exception). The
# operator-id source is broken out so tests can inject a fixed list.
SessionFactory = Callable[[], AbstractContextManager[Session]]
OperatorIdSource = Callable[[Session], Iterable[int]]


def default_operator_ids(session: Session) -> Iterable[int]:
    """Yield every operator id present in ``operator_profile``.

    Split from the tick body so it can be swapped in tests (and, later,
    filtered to "operators with a cookie on file" once the extra query
    proves useful).
    """
    yield from session.scalars(select(OperatorProfile.id)).all()


def run_heartbeat_tick(
    probe: HeartbeatProbe,
    session_factory: SessionFactory,
    *,
    operator_ids: OperatorIdSource = default_operator_ids,
) -> list[HeartbeatOutcome]:
    """Run one heartbeat probe for every operator; return the outcomes.

    Each probe uses its own session so failures are isolated. The
    outer session that enumerates operator ids is separate too, and
    closes before probes start — this keeps the iteration set stable
    even if a probe were to somehow mutate ``operator_profile``.
    """
    with session_factory() as session:
        # Materialise the id list before releasing the session so we
        # never iterate a closed cursor.
        ids = list(operator_ids(session))

    outcomes: list[HeartbeatOutcome] = []
    for operator_id in ids:
        try:
            with session_factory() as session:
                # Compute the previous heartbeat's timestamp BEFORE the
                # probe writes the new row. The alert evaluator uses it
                # to decide whether a fresh acknowledgment counts as
                # "since the last heartbeat" (US4.3 suppression rule).
                outcome = probe.run(session, operator_id)
                prev_at = previous_heartbeat_at(session, operator_id, outcome.probed_at)
                action = evaluate_cookie_expiring(
                    session=session,
                    operator_id=operator_id,
                    projected_ttl_at=outcome.projected_ttl_at,
                    now=outcome.probed_at,
                    previous_heartbeat_at=prev_at,
                )
                alert_id = apply_alert_action(
                    session, operator_id, action, now=outcome.probed_at
                )
        except NoCookieOnFile:
            # Normal state for a freshly seeded operator; skip quietly.
            _log.info("heartbeat.tick.skipped_no_cookie", operator_id=operator_id)
            continue
        except Exception as exc:
            # Never let one operator's failure abort the whole tick.
            _log.exception(
                "heartbeat.tick.probe_failed",
                operator_id=operator_id,
                error=str(exc),
            )
            continue

        _log.info(
            "heartbeat.tick.probe_completed",
            operator_id=operator_id,
            reading_id=outcome.reading_id,
            result=outcome.result,
            projected_ttl_at=(
                outcome.projected_ttl_at.isoformat()
                if outcome.projected_ttl_at
                else None
            ),
            alert_action=type(action).__name__,
            alert_id=alert_id,
        )
        outcomes.append(outcome)

    return outcomes


__all__ = [
    "OperatorIdSource",
    "SessionFactory",
    "default_operator_ids",
    "run_heartbeat_tick",
]
