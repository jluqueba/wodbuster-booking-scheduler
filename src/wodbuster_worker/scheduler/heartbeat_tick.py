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
from sqlalchemy.orm import Session

from ..heartbeat.alerts import (
    apply_alert_action,
    evaluate_cookie_expiring,
)
from ..heartbeat.probe import HeartbeatOutcome, HeartbeatProbe, NoCookieOnFile
from ..persistence.gym_accounts import list_active_gym_account_ids

_log = structlog.get_logger(__name__)

# Type aliases for the two injectables. The session factory follows
# ``persistence.engine.get_session``'s shape (context-manager yielding
# a Session that commits on success / rolls back on exception). The
# gym-account-id source is broken out so tests can inject a fixed list.
SessionFactory = Callable[[], AbstractContextManager[Session]]
GymAccountIdSource = Callable[[Session], Iterable[int]]


def default_gym_account_ids(session: Session) -> Iterable[int]:
    """Yield every active gym-account id.

    Split from the tick body so it can be swapped in tests (and, later,
    filtered to "accounts with a cookie on file" once the extra query
    proves useful).
    """
    yield from list_active_gym_account_ids(session)


def run_heartbeat_tick(
    probe: HeartbeatProbe,
    session_factory: SessionFactory,
    *,
    gym_account_ids: GymAccountIdSource = default_gym_account_ids,
) -> list[HeartbeatOutcome]:
    """Run one heartbeat probe for every gym account; return the outcomes.

    Each probe uses its own session so failures are isolated. The
    outer session that enumerates gym-account ids is separate too, and
    closes before probes start — this keeps the iteration set stable
    even if a probe were to somehow mutate ``gym_account``.
    """
    with session_factory() as session:
        # Materialise the id list before releasing the session so we
        # never iterate a closed cursor.
        ids = list(gym_account_ids(session))

    outcomes: list[HeartbeatOutcome] = []
    for gym_account_id in ids:
        try:
            with session_factory() as session:
                outcome = probe.run(session, gym_account_id)
                action = evaluate_cookie_expiring(
                    session=session,
                    gym_account_id=gym_account_id,
                    projected_ttl_at=outcome.projected_ttl_at,
                    now=outcome.probed_at,
                )
                alert_id = apply_alert_action(
                    session, gym_account_id, action, now=outcome.probed_at
                )
        except NoCookieOnFile:
            # Normal state for a freshly seeded gym account; skip quietly.
            _log.info("heartbeat.tick.skipped_no_cookie", gym_account_id=gym_account_id)
            continue
        except Exception as exc:
            # Never let one account's failure abort the whole tick.
            _log.exception(
                "heartbeat.tick.probe_failed",
                gym_account_id=gym_account_id,
                error=str(exc),
            )
            continue

        _log.info(
            "heartbeat.tick.probe_completed",
            gym_account_id=gym_account_id,
            reading_id=outcome.reading_id,
            result=outcome.result,
            projected_ttl_at=(
                outcome.projected_ttl_at.isoformat() if outcome.projected_ttl_at else None
            ),
            alert_action=type(action).__name__,
            alert_id=alert_id,
        )
        outcomes.append(outcome)

    return outcomes


__all__ = [
    "GymAccountIdSource",
    "SessionFactory",
    "default_gym_account_ids",
    "run_heartbeat_tick",
]
