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

from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..persistence.cookie_store import CookieStore
from ..persistence.models import BookingOutcome, GymAccount, NotificationOutbox, OperatorProfile
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
    gym_account_id: int,
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
    if booking is None or booking.gym_account_id != gym_account_id:
        # Never confirm existence to non-owners (CC-012).
        raise BookingNotFoundError(f"booking {booking_id} not found")

    if booking.terminal_status == "cancelled":
        # Idempotent short-circuit — no WodBuster call, no state change.
        _log.info(
            "booking.cancel.idempotent",
            gym_account_id=gym_account_id,
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

    cookie = cookie_store.load(session, gym_account_id)
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
        gym_account_id=gym_account_id,
        booking=booking,
        now=_now,
    )
    _log.info(
        "booking.cancel.persisted",
        gym_account_id=gym_account_id,
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
    gym_account_id: int,
    booking: BookingOutcome,
    now: datetime,
) -> None:
    """Add the banner + Telegram rows for the cancellation.

    Outbox delivery is user-scoped (ADR-0007): resolve the owning
    ``user_id`` from the gym account before enqueuing.
    """
    gym_account = session.get(GymAccount, gym_account_id)
    if gym_account is None:  # pragma: no cover - FK guarantees presence
        return
    user_id = gym_account.user_id
    text = f"Cancelled {booking.target_class} for {_format_slot(booking.target_slot)}."
    payload: dict[str, Any] = {
        "kind": "booking_result",
        "terminal_status": "cancelled",
        "outcome_id": int(booking.id),
        "class_type": booking.target_class,
        "target_slot": booking.target_slot.astimezone(UTC).isoformat(),
        "text": text,
    }
    session.add(
        NotificationOutbox(
            user_id=user_id,
            gym_account_id=gym_account_id,
            kind="banner",
            target=str(user_id),
            payload=payload,
            enqueued_at=now,
        )
    )
    operator = session.get(OperatorProfile, user_id)
    if operator is None or not operator.telegram_chat_id:
        return
    session.add(
        NotificationOutbox(
            user_id=user_id,
            gym_account_id=gym_account_id,
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
    # WodBuster's ``HoraComienzo`` and the booked ``rule.class_time`` are
    # the gym's local wall clock (Europe/Madrid by default). ``target_slot``
    # is stored in UTC, so render it back in the operator's timezone before
    # matching — using UTC here shifts the time by the offset (2h in summer)
    # and the class is never found ("no longer visible").
    from ..scheduler.rule_jobs import operator_timezone

    return target_slot.astimezone(operator_timezone()).strftime("%H:%M")


def _format_slot(target_slot: datetime) -> str:
    # Local import avoids a circular import (rule_jobs -> executor ->
    # vacation -> cancellation). Rendered in the operator's timezone.
    from ..scheduler.rule_jobs import operator_timezone

    return target_slot.astimezone(operator_timezone()).strftime("%a %d %b %H:%M %Z")


def list_recent_bookings(
    session: Session,
    gym_account_id: int,
    *,
    since: datetime | None = None,
    limit: int = 50,
) -> list[BookingOutcome]:
    """Return the operator's most recent booking attempts, newest first.

    Used by the history page (and, transitively, by the cancel
    button which lives on that page). ``since`` narrows the result to
    attempts made at or after that instant (the history page uses it
    to show only the current week). ``limit`` bounds the result so a
    long-lived operator doesn't ship megabytes of rows to the browser
    on every visit.
    """
    stmt = select(BookingOutcome).where(BookingOutcome.gym_account_id == gym_account_id)
    if since is not None:
        stmt = stmt.where(BookingOutcome.attempted_at >= since)
    stmt = stmt.order_by(BookingOutcome.attempted_at.desc()).limit(limit)
    return list(session.execute(stmt).scalars().all())


def list_upcoming_bookings(
    session: Session,
    gym_account_id: int,
    *,
    now: datetime | None = None,
    horizon_days: int = 14,
) -> list[BookingOutcome]:
    """Return granted bookings whose class start is in the future.

    Powers the "Upcoming" section on the history page: what the
    operator is going to attend next, chronologically. Filtered to
    ``granted`` terminals because cancelled/failed rows have nothing
    to attend. Bounded by ``horizon_days`` so a stray far-future
    booking cannot flood the section.
    """
    _now = now or datetime.now(tz=UTC)
    horizon = _now + timedelta(days=horizon_days)
    return list(
        session.execute(
            select(BookingOutcome)
            .where(
                BookingOutcome.gym_account_id == gym_account_id,
                BookingOutcome.terminal_status == "granted",
                BookingOutcome.target_slot >= _now,
                BookingOutcome.target_slot <= horizon,
            )
            .order_by(BookingOutcome.target_slot.asc())
        )
        .scalars()
        .all()
    )


def resolve_owner_gym_account(
    session: Session,
    *,
    user_id: int,
    booking_id: int,
) -> int | None:
    """Return the gym-account id that owns ``booking_id``, or ``None``.

    Scoped to ``user_id``'s ACTIVE gym accounts so a Telegram ``/cancel``
    resolves the owning gym across every gym the user has without
    confirming existence for bookings the user does not own (CC-012).
    """
    return session.scalars(
        select(BookingOutcome.gym_account_id)
        .join(GymAccount, GymAccount.id == BookingOutcome.gym_account_id)
        .where(
            BookingOutcome.id == booking_id,
            GymAccount.user_id == user_id,
            GymAccount.active.is_(True),
        )
    ).first()


__all__ = [
    "BookingAlreadyCancelledError",
    "BookingNotFoundError",
    "CancelClientProtocol",
    "CancellationUpstreamError",
    "cancel_booking",
    "list_recent_bookings",
    "list_upcoming_bookings",
    "resolve_owner_gym_account",
]
