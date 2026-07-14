"""One-shot cookie heartbeat probe (US4.1).

The probe runs on every scheduler tick (slice 2 will wire APScheduler
around it) but is deliberately kept side-effect-explicit so a route
handler, a manual invocation, or a test can drive it too.

Responsibility split:

- :class:`HeartbeatProbe` owns the transactional shape: load the
  operator's cookie, delegate the classification to
  :class:`CookieValidator`, write a :class:`HeartbeatReading` row, and
  update the freshness columns on :class:`CookieCredential`.
- Alert emission is **not** in this module. Slice 3 layers a separate
  ``AlertEvaluator`` on top of the :class:`HeartbeatOutcome` this class
  returns, so the probe stays testable without pulling in the outbox
  and next-window lookahead machinery.
- The pure projection maths live in :func:`heartbeat.estimator.project_ttl`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.cookie_store import CookieDecryptError, CookieStore
from ..persistence.models import CookieCredential, HeartbeatReading
from ..security.cookie import CookieValidator, Rejected, Unknown, Valid
from .estimator import project_ttl


class NoCookieOnFile(Exception):
    """Raised when a probe is triggered for an operator with no cookie.

    Distinct from a rejection: there is nothing to probe, so no
    ``heartbeat_reading`` row is written and no state changes. The
    scheduler treats this as "skip this operator" rather than as a
    failure.
    """


@dataclass(frozen=True)
class HeartbeatOutcome:
    """What a single probe wrote to the database.

    Downstream (slice 3's alert evaluator) reads this to decide whether
    to emit or clear alerts. Structured as a plain frozen dataclass so
    it can be passed across a serialisation boundary later (e.g. the
    scheduler emitting a log event or an APScheduler job listener).
    """

    operator_id: int
    reading_id: int
    result: Literal["valid", "rejected", "unknown"]
    probed_at: datetime
    projected_ttl_at: datetime | None


class HeartbeatProbe:
    """Runs a single heartbeat probe for one operator.

    Composition-friendly: takes a :class:`CookieStore` and a
    :class:`CookieValidator` at construction (the lifespan hook will
    build one instance and share it across the scheduler job and any
    debug routes).

    The ``ceiling`` argument is a ``timedelta`` (not a bare int of
    days) so tests can drive it in seconds and production reads it
    from ``Settings.cookie_projected_ttl_ceiling_days``.
    """

    __slots__ = ("_ceiling", "_store", "_validator")

    def __init__(
        self,
        store: CookieStore,
        validator: CookieValidator,
        *,
        ceiling: timedelta,
    ) -> None:
        self._store = store
        self._validator = validator
        self._ceiling = ceiling

    def run(
        self,
        session: Session,
        operator_id: int,
        *,
        now: datetime | None = None,
    ) -> HeartbeatOutcome:
        """Probe the cookie and persist the result.

        The caller owns the transaction: this method flushes the new
        row so the returned ``reading_id`` is populated, but does not
        commit. Composes cleanly with US-003's per-request session and
        with the scheduler job (which will commit its own scope).

        Raises :class:`NoCookieOnFile` when the operator has never
        pasted a cookie. Raises :class:`CookieDecryptError` when a row
        exists but is unreadable (mirrors :meth:`CookieStore.load`).
        """
        cookie_value = self._store.load(session, operator_id)
        if cookie_value is None:
            raise NoCookieOnFile(operator_id)

        # ``now`` is injected for testability. Real callers rely on the
        # default, which is timezone-aware UTC to match the
        # ``TIMESTAMPTZ`` columns.
        probed_at = now or datetime.now(tz=UTC)

        verdict = self._validator.validate(cookie_value)
        result = _verdict_to_result(verdict)

        # Read the current projection under the caller's session so the
        # estimator sees an up-to-date value in the same transaction.
        credential = session.execute(
            select(CookieCredential).where(CookieCredential.operator_id == operator_id)
        ).scalar_one()
        new_projection = project_ttl(
            verdict=verdict,
            now=probed_at,
            ceiling=self._ceiling,
            previous=credential.projected_ttl_at,
        )

        # Update the freshness columns first so the estimator's write
        # is visible to any subsequent ``load`` in the same transaction.
        credential.last_validated_at = probed_at
        credential.last_probe_status = result
        credential.projected_ttl_at = new_projection

        reading = HeartbeatReading(
            operator_id=operator_id,
            probed_at=probed_at,
            result=result,
            projected_ttl_at=new_projection,
            # ``alert_id`` stays null; slice 3's evaluator will backfill
            # it once alerts are wired.
        )
        session.add(reading)
        session.flush()  # populate reading.id without committing

        return HeartbeatOutcome(
            operator_id=operator_id,
            reading_id=int(reading.id),
            result=result,
            probed_at=probed_at,
            projected_ttl_at=new_projection,
        )


def _verdict_to_result(
    verdict: object,
) -> Literal["valid", "rejected", "unknown"]:
    """Map validator verdicts to ``heartbeat_reading.result`` enum values.

    The enum mirrors the verdicts one-to-one and lives at the schema
    layer; the mapping is trivial today but centralising it here means
    any future rename or split (e.g. a distinct ``expired`` result)
    lands in one place.
    """
    if isinstance(verdict, Valid):
        return "valid"
    if isinstance(verdict, Rejected):
        return "rejected"
    if isinstance(verdict, Unknown):
        return "unknown"
    raise TypeError(f"unsupported verdict type: {type(verdict).__name__}")


# Re-export so callers of :mod:`heartbeat.probe` can catch decrypt
# failures without reaching into :mod:`persistence.cookie_store`.
_ = CookieDecryptError


__all__ = ["HeartbeatOutcome", "HeartbeatProbe", "NoCookieOnFile"]
