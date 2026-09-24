"""Turn one day of the gym calendar into attendance rows (ADR-0013).

The network read and the database write are deliberately separate
functions. Fetching first and writing second means a failed upstream
call never opens a transaction, so INV-009 ("a failed capture leaves no
partial day recorded as captured") holds by construction rather than by
an exception handler, and a two hundred millisecond HTTP round trip
never holds a database connection open.

What is persisted is only the operator. The payload carries every
athlete's display name, profile link and photograph link for each class;
:func:`read_operator_states` drops all of it before this module sees it,
and nothing here logs a value taken from an athlete entry (INV-001).

A finished class never changes, so a day whose local date is already
past is captured once and marked final. Today is captured provisionally
and re-read on the next visit, which is also what lets a coach's later
removal correct itself.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from time import monotonic
from typing import Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from ..persistence.engine import get_session
from ..persistence.models import AttendanceDay, AttendanceRecord
from ..scheduler.clock import (
    local_date_for_slot,
    local_wall_time,
    midnight_utc_ticks,
    operator_timezone,
)
from ..wodbuster_client.client import (
    LoadClassResponse,
    WodBusterAuthError,
    WodBusterProtocolError,
    WodBusterTransportError,
)
from ..wodbuster_client.parsers import (
    OperatorClassState,
    extract_class_slots,
    read_operator_states,
)

_log = structlog.get_logger(__name__)

# Local wall time used only to derive the day's ``ticks``. Noon is far
# from both midnights, so no daylight-saving transition can push the
# derived UTC date onto a neighbouring day.
_TICKS_ANCHOR = "12:00"


class CaptureClientProtocol(Protocol):
    """The one client method capture needs.

    Deliberately narrow: capture is a read. A client passed here is not
    expected to expose ``inscribir`` or ``borrar``, and the tests assert
    that neither is ever called (INV-007).
    """

    def load_class(
        self, cookie_value: str, ticks: int
    ) -> LoadClassResponse:  # pragma: no cover - protocol only
        ...


@dataclass(frozen=True)
class DayCapture:
    """What one day of the gym calendar said, before it is persisted."""

    local_date: date
    # Class instances the gym ran that day, across all athletes. The
    # "was the gym open" signal, and the reason a closed day cannot
    # break a training streak.
    class_count: int
    states: tuple[OperatorClassState, ...]


@dataclass(frozen=True)
class DayCaptureResult:
    """What was written for one day."""

    local_date: date
    class_count: int
    is_final: bool
    records_written: int


def ticks_for_local_date(local_date: date) -> int:
    """Return the upstream ``ticks`` parameter for an operator-local day.

    Uses the executor's UTC-midnight convention (ADR-0012, Decision 7) so
    the calendar day captured is the same day a booking attempt would
    read, rather than the week-scoped picker's local-midnight one.
    """
    return midnight_utc_ticks(local_wall_time(local_date, _TICKS_ANCHOR))


def fetch_day(
    client: CaptureClientProtocol,
    *,
    cookie_value: str,
    local_date: date,
    operator_idu: str,
) -> DayCapture:
    """Read one day from the gym calendar. No database access.

    Client exceptions propagate untouched: the caller records nothing
    for a day it could not read, which is what keeps the ledger honest
    about coverage.
    """
    response = client.load_class(cookie_value, ticks_for_local_date(local_date))
    payload = response.payload
    states = read_operator_states(payload, operator_idu=operator_idu)
    capture = DayCapture(
        local_date=local_date,
        class_count=len(extract_class_slots(payload)),
        states=tuple(states),
    )
    _log.info(
        "statistics.capture.fetched",
        local_date=local_date.isoformat(),
        class_count=capture.class_count,
        operator_states=len(capture.states),
        latency_ms=round(response.latency_ms, 1),
    )
    return capture


def persist_day(
    session: Session,
    *,
    gym_account_id: int,
    capture: DayCapture,
    now: datetime | None = None,
) -> DayCaptureResult:
    """Write one captured day. The caller owns the transaction.

    Both writes are upserts keyed on the constraints declared in the
    models, so capturing the same day twice produces the same rows and
    two concurrent page loads cannot duplicate anything.
    """
    moment = now if now is not None else datetime.now(tz=UTC)
    is_final = capture.local_date < local_date_for_slot(moment)

    for state in capture.states:
        _upsert_record(
            session,
            gym_account_id=gym_account_id,
            local_date=capture.local_date,
            state=state,
        )
    _upsert_day(
        session,
        gym_account_id=gym_account_id,
        local_date=capture.local_date,
        class_count=capture.class_count,
        captured_at=moment,
        is_final=is_final,
    )

    _log.info(
        "statistics.capture.persisted",
        gym_account_id=gym_account_id,
        local_date=capture.local_date.isoformat(),
        class_count=capture.class_count,
        records=len(capture.states),
        is_final=is_final,
    )
    return DayCaptureResult(
        local_date=capture.local_date,
        class_count=capture.class_count,
        is_final=is_final,
        records_written=len(capture.states),
    )


def capture_day(
    session: Session,
    *,
    gym_account_id: int,
    local_date: date,
    client: CaptureClientProtocol,
    cookie_value: str,
    operator_idu: str,
    now: datetime | None = None,
) -> DayCaptureResult:
    """Fetch one day and persist it. The caller owns the transaction.

    The fetch happens before any write, so an upstream failure leaves
    the session untouched and the day stays absent from the ledger.
    """
    capture = fetch_day(
        client,
        cookie_value=cookie_value,
        local_date=local_date,
        operator_idu=operator_idu,
    )
    return persist_day(session, gym_account_id=gym_account_id, capture=capture, now=now)


def _upsert_record(
    session: Session,
    *,
    gym_account_id: int,
    local_date: date,
    state: OperatorClassState,
) -> None:
    values = {
        "gym_account_id": gym_account_id,
        "local_date": local_date,
        "wodbuster_class_id": state.class_id,
        "class_name": state.class_name,
        "class_type_id": state.class_type_id,
        "start_at": local_wall_time(local_date, state.hora_comienzo).astimezone(UTC),
        "state": state.state,
        "state_changed_at": _to_utc(state.state_changed_at),
        "reservation_type": state.reservation_type,
        "capacity": state.capacity,
        "occupancy": state.occupancy,
        "ever_full": state.ever_full,
    }
    statement = insert(AttendanceRecord).values(**values)
    session.execute(
        statement.on_conflict_do_update(
            constraint="uq_attendance_record_gym_class",
            # The identity columns are excluded: they are the conflict
            # target, and re-assigning them would be a no-op that reads
            # as if they could change.
            set_={
                key: statement.excluded[key]
                for key in values
                if key not in ("gym_account_id", "wodbuster_class_id")
            },
        )
    )


def _upsert_day(
    session: Session,
    *,
    gym_account_id: int,
    local_date: date,
    class_count: int,
    captured_at: datetime,
    is_final: bool,
) -> None:
    statement = insert(AttendanceDay).values(
        gym_account_id=gym_account_id,
        local_date=local_date,
        captured_at=captured_at,
        class_count=class_count,
        is_final=is_final,
    )
    session.execute(
        statement.on_conflict_do_update(
            constraint="uq_attendance_day_gym_date",
            set_={
                "captured_at": statement.excluded.captured_at,
                "class_count": statement.excluded.class_count,
                "is_final": statement.excluded.is_final,
            },
        )
    )


def _to_utc(naive: datetime | None) -> datetime | None:
    """Attach the operator timezone to an upstream instant, then convert.

    ``FechaEstado`` carries no timezone marker and is read as the gym's
    local wall clock, which is the same assumption the scheduler already
    makes about every published class time.
    """
    if naive is None:
        return None
    if naive.tzinfo is not None:
        return naive.astimezone(UTC)
    return naive.replace(tzinfo=operator_timezone()).astimezone(UTC)


@dataclass(frozen=True)
class CatchUpResult:
    """What one catch-up pass managed to do."""

    captured: tuple[date, ...]
    remaining: int
    stopped_early: bool


def pending_dates(
    session: Session,
    *,
    gym_account_id: int,
    today: date,
    horizon_days: int,
    priority: tuple[date, date] | None = None,
) -> list[date]:
    """Return the local dates still worth reading, in the order to read them.

    A day is worth reading when no final ledger row covers it. Today is
    always worth reading, because its ledger row is provisional until
    the day ends.

    ``priority`` is the range the reader is looking at. Those days come
    first: a visit to a month that has never been read should fill that
    month, not spend the request's budget on days the reader cannot
    see. Everything else follows, newest first, so the general history
    still fills in from the present backwards.
    """
    oldest = today - timedelta(days=horizon_days)
    final_dates = set(
        session.scalars(
            select(AttendanceDay.local_date).where(
                AttendanceDay.gym_account_id == gym_account_id,
                AttendanceDay.is_final.is_(True),
                AttendanceDay.local_date >= oldest,
            )
        ).all()
    )

    span = (today - oldest).days
    candidates = [
        day
        for offset in range(span + 1)
        if (day := today - timedelta(days=offset)) not in final_dates
    ]
    if priority is None:
        return candidates

    start, end = priority
    preferred = [day for day in candidates if start <= day <= end]
    rest = [day for day in candidates if not (start <= day <= end)]
    return preferred + rest


def catch_up(
    *,
    gym_account_id: int,
    client: CaptureClientProtocol,
    cookie_value: str,
    operator_idu: str,
    cap: int,
    horizon_days: int,
    now: datetime | None = None,
    priority: tuple[date, date] | None = None,
    budget_seconds: float | None = None,
) -> CatchUpResult:
    """Capture the days worth reading, up to ``cap`` of them.

    One transaction per day, so a failure on the fourth day keeps the
    first three and leaves the fourth absent from the ledger rather than
    rolling back work that succeeded.

    Bounded twice, by count and by wall clock. The count alone is not
    enough: it assumes every upstream call is fast, and the one day that
    matters is the day the gym is slow. ``budget_seconds`` is checked
    before each call, so a request can overrun by at most one call.

    Because a finished day never changes, nothing is ever lost by
    capturing it late. Stopping early costs freshness and never
    correctness, which is what lets this run on a page request at all.
    """
    moment = now if now is not None else datetime.now(tz=UTC)
    today = local_date_for_slot(moment)
    started = monotonic()

    with get_session() as session:
        candidates = pending_dates(
            session,
            gym_account_id=gym_account_id,
            today=today,
            horizon_days=horizon_days,
            priority=priority,
        )

    captured: list[date] = []
    stopped_early = False
    for local_date in candidates[:cap]:
        if budget_seconds is not None and monotonic() - started >= budget_seconds:
            _log.info(
                "statistics.capture.budget_exhausted",
                gym_account_id=gym_account_id,
                captured=len(captured),
            )
            stopped_early = True
            break
        try:
            with get_session() as session:
                capture_day(
                    session,
                    gym_account_id=gym_account_id,
                    local_date=local_date,
                    client=client,
                    cookie_value=cookie_value,
                    operator_idu=operator_idu,
                    now=moment,
                )
        except (WodBusterAuthError, WodBusterProtocolError, WodBusterTransportError) as exc:
            _log.warning(
                "statistics.capture.upstream_error",
                gym_account_id=gym_account_id,
                local_date=local_date.isoformat(),
                error_type=type(exc).__name__,
            )
            stopped_early = True
            break
        captured.append(local_date)

    remaining = max(len(candidates) - len(captured), 0)
    return CatchUpResult(
        captured=tuple(captured),
        remaining=remaining,
        stopped_early=stopped_early or remaining > 0,
    )


__all__ = [
    "CaptureClientProtocol",
    "CatchUpResult",
    "DayCapture",
    "DayCaptureResult",
    "capture_day",
    "catch_up",
    "fetch_day",
    "pending_dates",
    "persist_day",
    "ticks_for_local_date",
]
