"""Cancellation service (US6.1).

Cancels a single granted booking on the operator's behalf. The
operator can invoke this from the web history page or (later) from
Telegram; both surfaces call into :func:`cancel_booking`.

Contract:

- Load the operator's ``booking_outcome`` row by id. 404-equivalent
  (``BookingNotFoundError``) when the row is missing or belongs to
  another operator (CC-012 isolation).
- If ``terminal_status`` is already ``cancelled``, short-circuit
  and return the row unchanged (CC-015 idempotency). No WodBuster
  call is issued.
- Otherwise call :meth:`WodBusterClient.borrar` with the row's
  ``rule_id``-derived class id. Rule-model-v2 stores the class type
  and class time on the rule, but *not* the WodBuster class id
  (that's ephemeral). We re-derive by fetching LoadClass for the
  target week and matching the same ``(class_type, class_time)``
  pair that produced the booking.
- Persist the ``cancelled`` terminal in the same transaction that
  writes the paired notification-outbox row (plan cross-cutting
  rule).

Error surface:

- ``BookingNotFoundError`` — 404 at the route layer.
- ``BookingAlreadyCancelledError`` — the caller treats as an
  informational no-op; the row is returned as-is.
- ``CancellationUpstreamError`` — WodBuster failed (auth, transport,
  protocol). The booking row is *not* mutated so a retry stays
  meaningful.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.cookie_store import CookieStore
from ..persistence.models import BookingOutcome, NotificationOutbox, OperatorProfile
from ..wodbuster_client.client import (
    BookingActionResponse,
    LoadClassResponse,
    WodBusterAuthError,
    WodBusterProtocolError,
    WodBusterTransportError,
)
from ..wodbuster_client.parsers import extract_class_slots, find_matching_slot

_log = structlog.get_logger(__name__)


class BookingNotFoundError(Exception):
    """Raised when the booking row does not exist or is not owned."""


class BookingAlreadyCancelledError(Exception):
    """Signals idempotent short-circuit — no WodBuster call issued."""


class CancellationUpstreamError(Exception):
    """WodBuster refused the cancel call; the row is unchanged."""


class CancelClientProtocol(Protocol):
    """WodBuster surface used by the cancellation service.

    Structural type so tests can pass a fake without inheriting.
    """

    def load_class(
        self, cookie_value: str, ticks: int
    ) -> LoadClassResponse:  # pragma: no cover - protocol only
        ...

    def borrar(  # pragma: no cover - protocol only
        self,
        cookie_value: str,
        *,
        class_id: str | int,
        ticks: int,
    ) -> BookingActionResponse: ...


def cancel_booking(
    session: Session,
    *,
    operator_id: int,
    booking_id: int,
    client: CancelClientProtocol,
    cookie_store: CookieStore,
    now: datetime | None = None,
) -> BookingOutcome:
    """Cancel one booking. Caller commits the session.

    Returns the persisted :class:`BookingOutcome`. Raises the typed
    exceptions above for expected failure modes so the route layer
    can map them to HTTP responses.
    """
    _now = now or datetime.now(tz=UTC)

    booking = session.get(BookingOutcome, booking_id)
    if booking is None or booking.operator_id != operator_id:
        # Never confirm existence to non-owners (CC-012).
        raise BookingNotFoundError(f"booking {booking_id} not found")

    if booking.terminal_status == "cancelled":
        # Idempotent short-circuit — no WodBuster call, no state change.
        _log.info(
            "booking.cancel.idempotent",
            operator_id=operator_id,
            booking_id=booking_id,
        )
        raise BookingAlreadyCancelledError(f"booking {booking_id} already cancelled")

    if booking.terminal_status != "granted":
        # Nothing to undo — the booking never succeeded. Treat as
        # not-found from the caller's perspective so the UI shows the
        # standard "already handled" flow.
        raise BookingAlreadyCancelledError(
            f"booking {booking_id} is {booking.terminal_status!r}, not granted"
        )

    cookie = cookie_store.load(session, operator_id)
    if cookie is None:
        raise CancellationUpstreamError("no cookie on file")

    ticks = _midnight_utc_ticks(booking.target_slot)
    class_id = _resolve_class_id(
        client=client,
        cookie=cookie,
        ticks=ticks,
        class_type=booking.target_class,
        class_time=_hhmm_from_datetime(booking.target_slot),
    )
    if class_id is None:
        raise CancellationUpstreamError(
            f"class {booking.target_class!r} at "
            f"{_hhmm_from_datetime(booking.target_slot)} no longer visible"
        )

    try:
        response = client.borrar(cookie, class_id=class_id, ticks=ticks)
    except WodBusterAuthError as exc:
        raise CancellationUpstreamError(f"auth error: {exc}") from exc
    except (WodBusterTransportError, WodBusterProtocolError) as exc:
        raise CancellationUpstreamError(f"upstream: {exc}") from exc

    # WodBuster's borrar handler returns the same Res vocabulary as
    # inscribir. "granted" here means "cancel accepted"; anything
    # else is a soft failure the operator will see reflected in the
    # persisted row.
    if response.outcome not in {"granted", "unknown"}:
        raise CancellationUpstreamError(f"WodBuster refused cancel: {response.raw_res!r}")

    booking.terminal_status = "cancelled"
    booking.notified_at = None  # re-notify on the new terminal
    booking.response_payload = f"cancelled by operator; borrar Res={response.raw_res!r}"

    _enqueue_cancel_outbox(
        session,
        operator_id=operator_id,
        booking=booking,
        now=_now,
    )
    _log.info(
        "booking.cancel.persisted",
        operator_id=operator_id,
        booking_id=booking_id,
        raw_res=response.raw_res,
    )
    return booking


def _resolve_class_id(
    *,
    client: CancelClientProtocol,
    cookie: str,
    ticks: int,
    class_type: str,
    class_time: str,
) -> int | None:
    """Fetch LoadClass and pick the class instance matching the booking."""
    try:
        loaded = client.load_class(cookie, ticks)
    except WodBusterAuthError as exc:
        raise CancellationUpstreamError(f"auth error: {exc}") from exc
    except (WodBusterTransportError, WodBusterProtocolError) as exc:
        raise CancellationUpstreamError(f"upstream: {exc}") from exc

    slot = find_matching_slot(
        extract_class_slots(loaded.payload),
        class_type=class_type,
        class_time=class_time,
    )
    if slot is None:
        return None
    return slot.id


def _enqueue_cancel_outbox(
    session: Session,
    *,
    operator_id: int,
    booking: BookingOutcome,
    now: datetime,
) -> None:
    """Add the banner + Telegram rows for the cancellation."""
    text = f"Cancelled {booking.target_class} for {_format_slot(booking.target_slot)}."
    payload: dict[str, Any] = {
        "kind": "booking_result",
        "terminal_status": "cancelled",
        "outcome_id": int(booking.id),
        "text": text,
    }
    session.add(
        NotificationOutbox(
            operator_id=operator_id,
            kind="banner",
            target=str(operator_id),
            payload=payload,
            enqueued_at=now,
        )
    )
    operator = session.get(OperatorProfile, operator_id)
    if operator is None or not operator.telegram_chat_id:
        return
    session.add(
        NotificationOutbox(
            operator_id=operator_id,
            kind="telegram",
            target=operator.telegram_chat_id,
            payload=payload,
            enqueued_at=now,
        )
    )


def _midnight_utc_ticks(target_slot: datetime) -> int:
    aware = target_slot.astimezone(UTC)
    midnight = aware.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _hhmm_from_datetime(target_slot: datetime) -> str:
    return target_slot.astimezone(UTC).strftime("%H:%M")


def _format_slot(target_slot: datetime) -> str:
    return target_slot.astimezone(UTC).strftime("%a %d %b %H:%M UTC")


def list_recent_bookings(
    session: Session,
    operator_id: int,
    *,
    limit: int = 50,
) -> list[BookingOutcome]:
    """Return the operator's most recent booking attempts, newest first.

    Used by the history page (and, transitively, by the cancel
    button which lives on that page). ``limit`` bounds the result so
    a long-lived operator doesn't ship megabytes of rows to the
    browser on every visit.
    """
    return list(
        session.execute(
            select(BookingOutcome)
            .where(BookingOutcome.operator_id == operator_id)
            .order_by(BookingOutcome.attempted_at.desc())
            .limit(limit)
        )
        .scalars()
        .all()
    )


__all__ = [
    "BookingAlreadyCancelledError",
    "BookingNotFoundError",
    "CancelClientProtocol",
    "CancellationUpstreamError",
    "cancel_booking",
    "list_recent_bookings",
]
