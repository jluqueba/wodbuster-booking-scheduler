"""Telegram chat-binding token store (US9.8).

The operator's web UI mints a short-lived one-time token; the
operator DMs the bot ``/start <token>``; the webhook handler looks
the token up and writes ``operator_profile.telegram_chat_id`` on
the matching operator so the notification dispatcher can start
delivering to that chat.

In-memory store is sufficient for the single-replica deployment:
tokens live 10 minutes by default, the operator uses each one
once, and a process restart just invalidates any pending token
(the operator generates a fresh one). Multi-replica would need a
DB-backed store — flagged in the docstring so a future reader
does not casually add a second replica.

Thread-safe: the dispatcher runs on APScheduler's thread pool and
FastAPI on the ASGI pool, so mutation is guarded by a lock. All
reads consume-and-delete so a stolen token can only be used once.
"""

from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

DEFAULT_TOKEN_TTL = timedelta(minutes=10)
_TOKEN_BYTES = 16  # ~22 URL-safe chars


@dataclass(frozen=True)
class _Entry:
    operator_id: int
    expires_at: datetime


class TelegramBindStore:
    """One-shot bind-token bag with TTL.

    Lifecycle:

    - :meth:`issue` mints a fresh token for ``operator_id`` and
      remembers it until ``ttl`` elapses.
    - :meth:`consume` looks a token up, removes it if present and
      unexpired, and returns the ``operator_id``. Returns ``None``
      on miss or expiry — the webhook responds with a helpful
      "token invalid or expired" message either way.
    - :meth:`purge_expired` is called opportunistically from
      :meth:`issue` and :meth:`consume` so the map stays small
      without a background job.
    """

    def __init__(self, *, ttl: timedelta = DEFAULT_TOKEN_TTL) -> None:
        self._ttl = ttl
        self._store: dict[str, _Entry] = {}
        self._lock = threading.Lock()

    def issue(
        self,
        operator_id: int,
        *,
        now: datetime | None = None,
    ) -> str:
        """Mint and remember a fresh token for ``operator_id``."""
        _now = now or datetime.now(tz=UTC)
        token = secrets.token_urlsafe(_TOKEN_BYTES)
        with self._lock:
            self._purge_expired_locked(_now)
            self._store[token] = _Entry(
                operator_id=operator_id,
                expires_at=_now + self._ttl,
            )
        return token

    def consume(
        self,
        token: str,
        *,
        now: datetime | None = None,
    ) -> int | None:
        """Return ``operator_id`` for a valid token; remove it either way."""
        _now = now or datetime.now(tz=UTC)
        with self._lock:
            self._purge_expired_locked(_now)
            entry = self._store.pop(token, None)
        if entry is None:
            return None
        if entry.expires_at <= _now:
            return None
        return entry.operator_id

    def _purge_expired_locked(self, now: datetime) -> None:
        """Drop expired entries. Caller must hold ``self._lock``."""
        expired = [k for k, v in self._store.items() if v.expires_at <= now]
        for k in expired:
            self._store.pop(k, None)


__all__ = ["DEFAULT_TOKEN_TTL", "TelegramBindStore"]
