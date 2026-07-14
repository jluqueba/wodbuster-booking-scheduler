"""Notification outbox dispatcher (US2.1, US2.3).

Poll ``notification_outbox`` for pending rows and hand each one to
its channel dispatcher (Telegram, banner). Runs inside APScheduler's
thread pool alongside the heartbeat tick — one dispatcher tick per 5
seconds is the plan default.

Consistency contract:

- **Every state-mutating write commits its outbox row in the same
  transaction.** The dispatcher's read-then-mark cycle assumes that
  guarantee. See ``heartbeat/alerts.py`` for the primary producer.
- **A pending row = ``dispatched_at IS NULL``.** Once marked, the row
  is done regardless of `attempt_count`.
- **Retries are bounded by ``max_attempts``.** On the last failed
  attempt the row is marked ``dispatched_at`` too, with an
  ``exhausted=true`` marker in the payload, so we do not loop
  forever on a permanently broken destination.
- **Banner "delivery" is a no-op.** The producer already writes the
  ``alert`` row that the dashboard reads; the outbox row exists only
  so both channels share the same durable-write contract. The
  dispatcher marks it dispatched immediately.
- **Ordering.** Rows are dispatched in ``id`` order (approximately
  enqueue order) so a burst of alerts arrives in the same sequence
  on Telegram as it appears in the banner.

The dispatcher intentionally does **not** manage exponential backoff
timing inside a single tick — the 5-second poll cadence, combined
with ``max_attempts`` acting as a hard cap, gives us "try roughly
every 5 seconds up to N times" behaviour. Full per-row backoff
timing lands in a later slice if we start seeing Telegram 429s.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..observability import telemetry
from ..persistence.models import NotificationOutbox, OperatorProfile
from . import telegram

_log = structlog.get_logger(__name__)

# Session factory type. Matches ``persistence.engine.get_session`` and
# is also what the heartbeat wiring passes.
SessionFactory = Callable[[], AbstractContextManager[Session]]

# Telegram sender type. Kept injectable so tests can supply a fake
# that captures calls instead of touching the network.
TelegramSender = Callable[..., None]

# Default retry ceiling. Chosen small so a permanently broken
# destination stops churning quickly; production callers can override
# via ``NotificationDispatcher(max_attempts=...)``.
_DEFAULT_MAX_ATTEMPTS = 5


class NotificationDispatcher:
    """Polls the outbox and drives channel dispatchers."""

    def __init__(
        self,
        *,
        bot_token: str | None,
        session_factory: SessionFactory,
        max_attempts: int = _DEFAULT_MAX_ATTEMPTS,
        telegram_sender: TelegramSender = telegram.send_message,
    ) -> None:
        self._bot_token = bot_token
        self._session_factory = session_factory
        self._max_attempts = max_attempts
        self._telegram_sender = telegram_sender

    def tick(self) -> None:
        """Drain one batch of pending outbox rows.

        Each row is handled in its own transaction so a slow Telegram
        call does not hold locks on later rows.
        """
        pending_ids = self._load_pending_ids()
        for row_id in pending_ids:
            self._handle_one(row_id)

    def _load_pending_ids(self) -> list[int]:
        with self._session_factory() as session:
            rows = session.execute(
                select(NotificationOutbox.id)
                .where(NotificationOutbox.dispatched_at.is_(None))
                .order_by(NotificationOutbox.id)
            ).all()
            return [row[0] for row in rows]

    def _handle_one(self, row_id: int) -> None:
        with self._session_factory() as session:
            row = session.get(NotificationOutbox, row_id)
            if row is None or row.dispatched_at is not None:
                # Another tick or a delete raced us. Nothing to do.
                return

            attempt_number = row.attempt_count + 1
            try:
                if row.kind == "telegram":
                    self._dispatch_telegram(session, row)
                elif row.kind == "banner":
                    # No wire delivery — the alert row already backs
                    # the banner UI. Mark done.
                    pass
                else:
                    _log.warning(
                        "dispatcher.unknown_kind",
                        row_id=row_id,
                        kind=row.kind,
                    )
                    self._mark_exhausted(row, reason=f"unknown kind {row.kind!r}")
                    session.commit()
                    return
            except telegram.TransientTelegramError as exc:
                row.attempt_count = attempt_number
                if attempt_number >= self._max_attempts:
                    self._mark_exhausted(row, reason=str(exc))
                    _log.warning(
                        "dispatcher.attempt_exhausted",
                        row_id=row_id,
                        kind=row.kind,
                        attempts=attempt_number,
                    )
                else:
                    _log.info(
                        "dispatcher.attempt_transient_failure",
                        row_id=row_id,
                        kind=row.kind,
                        attempts=attempt_number,
                        error=str(exc),
                    )
                session.commit()
                return
            except telegram.PermanentTelegramError as exc:
                row.attempt_count = attempt_number
                self._mark_exhausted(row, reason=str(exc))
                _log.warning(
                    "dispatcher.attempt_permanent_failure",
                    row_id=row_id,
                    kind=row.kind,
                    error=str(exc),
                )
                session.commit()
                return

            row.attempt_count = attempt_number
            row.dispatched_at = datetime.now(tz=UTC)
            session.commit()
            # US2.6 dispatch-lag metric: seconds between the outbox
            # row being enqueued and it finally leaving the queue.
            # Tag with kind so telegram vs banner buckets are
            # separable in Application Insights.
            try:
                lag = (row.dispatched_at - row.enqueued_at).total_seconds()
                telemetry.notification_dispatch_lag_seconds().record(lag, {"kind": row.kind})
            except Exception:  # pragma: no cover - metric emit must not raise
                pass
            _log.info(
                "dispatcher.attempt_ok",
                row_id=row_id,
                kind=row.kind,
                attempts=attempt_number,
            )

    def _dispatch_telegram(self, session: Session, row: NotificationOutbox) -> None:
        """Resolve the operator's chat id and send."""
        if not self._bot_token:
            # No bot token configured — nothing this tick can do.
            # Treat as a transient condition so the row stays pending
            # until the operator seeds the secret.
            raise telegram.TransientTelegramError("bot token not configured")

        chat_id = self._resolve_chat_id(session, row)
        text = _render_telegram_text(row.payload)
        self._telegram_sender(
            bot_token=self._bot_token,
            chat_id=chat_id,
            text=text,
        )

    def _resolve_chat_id(self, session: Session, row: NotificationOutbox) -> str:
        """Return the chat id to send to.

        The producer already stores the target on the outbox row.
        When that target is empty we fall back to the operator's
        registered ``telegram_chat_id`` — this keeps the alert
        producers slim (they do not need to look up the operator).
        """
        if row.target:
            return row.target
        op = session.get(OperatorProfile, row.operator_id)
        if op is None or not op.telegram_chat_id:
            raise telegram.PermanentTelegramError(
                f"operator {row.operator_id} has no telegram_chat_id"
            )
        return op.telegram_chat_id

    def _mark_exhausted(self, row: NotificationOutbox, *, reason: str) -> None:
        """Mark a row as dispatched even though it failed.

        Stops the polling loop from re-issuing the same request
        forever. The dispatch reason lands inside the payload for
        future post-mortem queries.
        """
        marker = dict(row.payload or {})
        marker["exhausted"] = True
        marker["exhausted_reason"] = reason[:500]
        row.payload = marker
        row.dispatched_at = datetime.now(tz=UTC)


def _render_telegram_text(payload: dict[str, Any] | None) -> str:
    """Turn an outbox payload into a human-readable Telegram body.

    Producers may pre-render a ``text`` field for one-off alerts; when
    that is set we prefer it. Otherwise the alert ``kind`` drives a
    kind-specific template so the operator gets an actionable message
    instead of a JSON dump.
    """
    payload = payload or {}
    text = payload.get("text")
    if isinstance(text, str) and text.strip():
        return text

    kind = payload.get("kind")
    if kind == "cookie_expiring":
        window = payload.get("next_window_at", "?")
        ttl = payload.get("projected_ttl_at", "?")
        return (
            "WodBuster cookie expiring before the next booking window "
            f"({window}). Projected TTL: {ttl}. "
            "Refresh the cookie at /cookie to keep the worker running."
        )
    if kind == "cookie_invalid":
        return (
            "WodBuster cookie was rejected. Bookings are paused until "
            "you paste a fresh cookie at /cookie."
        )
    if kind == "heartbeat_anomaly":
        window = payload.get("window_close_expected", "?")
        return (
            f"Heartbeat anomaly: no outcome recorded for the booking "
            f"window that should have closed by {window}. "
            "Check the worker."
        )

    # Not user-facing pretty; a safety net for unknown kinds.
    return f"[alert] {payload!r}"


__all__ = [
    "NotificationDispatcher",
    "SessionFactory",
    "TelegramSender",
]
