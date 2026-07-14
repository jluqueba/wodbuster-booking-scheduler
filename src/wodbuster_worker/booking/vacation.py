"""Vacation-mode service (US7.1, FR-015).

Two responsibilities:

- :func:`enable` opens a ``vacation_window`` row for ``[start, end]``
  and walks every granted ``booking_outcome`` whose ``target_slot``
  falls inside that range through :func:`cancel_booking`. The bulk
  cancel is *best-effort*: a per-booking upstream failure is
  logged, the outbox already carries the "cancelled" signal for the
  successful ones, and the window still opens so the scheduler
  skip-guard (see :func:`find_covering_window`) takes over for
  future runs.

- :func:`find_covering_window` is the read-side helper the booking
  executor calls before every attempt. Returns the covering
  vacation row (open, not closed) whose ``[start_date, end_date]``
  covers the given ``target_slot``, or ``None``.

The ``end_date`` boundary is *inclusive* by wall-clock date: a
vacation ``[2026-07-20, 2026-07-25]`` covers every class up to and
including 2026-07-25 23:59:59. That matches the plan's language
("inclusive start, inclusive end") and the way an operator writes
"holiday from 20th through 25th".

Row lifecycle:

- ``created_at`` — insertion time.
- ``closed_at`` — set by :func:`close_early` when the operator
  ends the window before ``end_date``; ``None`` while the window
  is open, populated on close (auto-close on end-date passing is
  read-only — no background job needed because the skip-guard
  filters by ``now <= end_date`` in :func:`find_covering_window`).
"""

from __future__ import annotations

from datetime import UTC, datetime, time

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.cookie_store import CookieStore
from ..persistence.models import BookingOutcome, VacationWindow
from .cancellation import (
    BookingAlreadyCancelledError,
    CancelClientProtocol,
    CancellationUpstreamError,
    cancel_booking,
)

_log = structlog.get_logger(__name__)


class VacationRangeError(ValueError):
    """Raised when ``start_date`` is after ``end_date``."""


class VacationNotFoundError(Exception):
    """Raised when the target window is missing or not owned."""


def enable(
    session: Session,
    *,
    operator_id: int,
    start_date: datetime,
    end_date: datetime,
    client: CancelClientProtocol,
    cookie_store: CookieStore,
    now: datetime | None = None,
) -> VacationWindow:
    """Open a vacation window and bulk-cancel granted bookings inside it.

    ``start_date`` and ``end_date`` are timezone-aware datetimes; the
    stored range is normalized so ``start_date`` collapses to
    midnight-UTC of that day and ``end_date`` extends to the last
    microsecond of that day (inclusive end).

    The caller owns the outer transaction. ``enable`` uses the
    session in a fine-grained way so a bulk-cancel that partially
    fails still commits the window + successful cancels via
    per-booking sub-transactions (see :meth:`Session.begin_nested`).
    """
    _now = now or datetime.now(tz=UTC)

    if start_date.tzinfo is None or end_date.tzinfo is None:
        raise VacationRangeError(
            "start_date and end_date must be timezone-aware"
        )
    normalized_start = _floor_day(start_date)
    normalized_end = _ceil_day(end_date)
    if normalized_start > normalized_end:
        raise VacationRangeError(
            "start_date must be on or before end_date"
        )

    window = VacationWindow(
        operator_id=operator_id,
        start_date=normalized_start,
        end_date=normalized_end,
        created_at=_now,
    )
    session.add(window)
    session.flush()  # populate window.id for logs

    granted = _load_granted_bookings_in_range(
        session,
        operator_id=operator_id,
        start=normalized_start,
        end=normalized_end,
        now=_now,
    )
    for booking in granted:
        try:
            with session.begin_nested():
                cancel_booking(
                    session,
                    operator_id=operator_id,
                    booking_id=int(booking.id),
                    client=client,
                    cookie_store=cookie_store,
                    now=_now,
                )
        except BookingAlreadyCancelledError:
            # Already cancelled — nothing to do; the log line inside
            # ``cancel_booking`` covers the audit trail.
            continue
        except CancellationUpstreamError as exc:
            # Log and move on. The window is still open so the
            # scheduler skip-guard prevents *future* attempts; the
            # operator has to intervene manually on this booking
            # (WodBuster couldn't process the cancel right now).
            _log.warning(
                "vacation.bulk_cancel.upstream_error",
                operator_id=operator_id,
                booking_id=int(booking.id),
                window_id=int(window.id),
                error=str(exc),
            )

    _log.info(
        "vacation.enabled",
        operator_id=operator_id,
        window_id=int(window.id),
        start=normalized_start.isoformat(),
        end=normalized_end.isoformat(),
        bulk_cancel_candidates=len(granted),
    )
    return window


def close_early(
    session: Session,
    *,
    operator_id: int,
    window_id: int,
    now: datetime | None = None,
) -> VacationWindow:
    """End an open vacation window early.

    Sets ``closed_at`` on the row. Idempotent: closing an already-
    closed window returns the row unchanged. Raises
    :class:`VacationNotFoundError` when the id is missing or belongs
    to another operator (CC-012).
    """
    _now = now or datetime.now(tz=UTC)
    window = session.get(VacationWindow, window_id)
    if window is None or window.operator_id != operator_id:
        raise VacationNotFoundError(f"vacation window {window_id} not found")
    if window.closed_at is not None:
        return window
    window.closed_at = _now
    _log.info(
        "vacation.closed_early",
        operator_id=operator_id,
        window_id=window_id,
    )
    return window


def list_open(
    session: Session,
    operator_id: int,
    *,
    now: datetime | None = None,
) -> list[VacationWindow]:
    """Return the operator's currently-effective vacation windows.

    "Open" means ``closed_at IS NULL`` AND ``now <= end_date``.
    Ordered by ``start_date`` ascending so the UI can show them
    calendar-style.
    """
    _now = now or datetime.now(tz=UTC)
    return list(
        session.execute(
            select(VacationWindow)
            .where(
                VacationWindow.operator_id == operator_id,
                VacationWindow.closed_at.is_(None),
                VacationWindow.end_date >= _now,
            )
            .order_by(VacationWindow.start_date.asc())
        )
        .scalars()
        .all()
    )


def find_covering_window(
    session: Session,
    *,
    operator_id: int,
    target_slot: datetime,
    now: datetime | None = None,
) -> VacationWindow | None:
    """Return the vacation window covering ``target_slot`` if any.

    Covers means ``start_date <= target_slot <= end_date`` on an
    *open* window (``closed_at IS NULL`` AND ``now <= end_date``).
    Returns ``None`` when the slot is not inside any vacation
    range — the caller then proceeds with the booking attempt.

    The skip-guard boundary semantics: inclusive on both sides.
    ``end_date`` is stored as ``23:59:59.999999`` of the last
    vacation day (see :func:`_ceil_day`), so a class at 21:30 of
    that day is still covered.
    """
    _now = now or datetime.now(tz=UTC)
    if target_slot.tzinfo is None:
        raise ValueError("target_slot must be timezone-aware")
    return session.execute(
        select(VacationWindow)
        .where(
            VacationWindow.operator_id == operator_id,
            VacationWindow.closed_at.is_(None),
            VacationWindow.end_date >= _now,
            VacationWindow.start_date <= target_slot,
            VacationWindow.end_date >= target_slot,
        )
        .order_by(VacationWindow.start_date.asc())
        .limit(1)
    ).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _floor_day(dt: datetime) -> datetime:
    """Return midnight of ``dt``'s date in UTC (inclusive start)."""
    aware = dt.astimezone(UTC)
    return datetime.combine(aware.date(), time.min, tzinfo=UTC)


def _ceil_day(dt: datetime) -> datetime:
    """Return the last microsecond of ``dt``'s date in UTC (inclusive end).

    Storing the ceiling here rather than at query time avoids
    off-by-one bugs on the read side: any target-slot comparison
    just uses ``<=`` against ``end_date``.
    """
    aware = dt.astimezone(UTC)
    end_of_day = datetime.combine(aware.date(), time.max, tzinfo=UTC)
    # ``time.max`` is ``23:59:59.999999`` — precise enough for the
    # skip-guard: booking slots never sit on the last microsecond.
    return end_of_day


def _load_granted_bookings_in_range(
    session: Session,
    *,
    operator_id: int,
    start: datetime,
    end: datetime,
    now: datetime,
) -> list[BookingOutcome]:
    """Return granted bookings whose ``target_slot`` sits in ``[start, end]``.

    Excludes bookings whose class already started before ``now``
    (nothing to cancel there — WodBuster would reject the ``borrar``
    call for a past class anyway).
    """
    return list(
        session.execute(
            select(BookingOutcome)
            .where(
                BookingOutcome.operator_id == operator_id,
                BookingOutcome.terminal_status == "granted",
                BookingOutcome.target_slot >= start,
                BookingOutcome.target_slot <= end,
                BookingOutcome.target_slot >= now,
            )
            .order_by(BookingOutcome.target_slot.asc())
        )
        .scalars()
        .all()
    )


__all__ = [
    "VacationNotFoundError",
    "VacationRangeError",
    "close_early",
    "enable",
    "find_covering_window",
    "list_open",
]
