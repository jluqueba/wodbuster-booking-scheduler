"""Telegram Bot API sender (US2.2).

Deliberately narrow surface: one function that POSTs a rendered
message to the Bot API and returns success/failure. No incoming
webhook, no update polling — those live in a later slice (the plan
lists a ``/telegram/webhook`` route for interactive commands as
US-006/US-007 territory).

Plan deviation: tasks.md lists ``python-telegram-bot`` as the
dependency; this module uses ``httpx`` (already in the stack) with
one direct call to ``sendMessage``. python-telegram-bot ships an
asyncio-first client that would force us to wrap every dispatcher
tick in ``asyncio.run(...)``. A single sync POST inside a thread-
based scheduler job is the smaller correct move; we can swap up if
we ever need Bot API features beyond ``sendMessage``.

Backoff strategy: :func:`send_message` raises ``TransientTelegramError``
on network / 5xx / 429 responses and ``PermanentTelegramError`` on
4xx (bad chat id, revoked token). The dispatcher retries only on
transient errors; permanent errors mark the outbox row failed and
open a ``cookie_invalid``-style alert in a future slice.
"""

from __future__ import annotations

import httpx
import structlog

_log = structlog.get_logger(__name__)

_API_ROOT = "https://api.telegram.org"
_SEND_TIMEOUT_SECONDS = 10.0


class TelegramError(Exception):
    """Base class for Telegram delivery failures."""


class TransientTelegramError(TelegramError):
    """Retryable: network hiccup, 5xx, or 429 (rate limited)."""


class PermanentTelegramError(TelegramError):
    """Non-retryable: 4xx from the Bot API (bad chat / token)."""


def send_message(
    *,
    bot_token: str,
    chat_id: str,
    text: str,
    client: httpx.Client | None = None,
) -> None:
    """POST a message to ``chat_id`` on behalf of ``bot_token``.

    ``client`` is injectable so tests can pass an
    ``httpx.MockTransport``-backed client. Production callers pass
    ``None`` and this function opens (and closes) its own client.
    """
    url = f"{_API_ROOT}/bot{bot_token}/sendMessage"
    payload = {"chat_id": chat_id, "text": text}

    owned_client = client is None
    http = client or httpx.Client(timeout=_SEND_TIMEOUT_SECONDS)
    try:
        try:
            response = http.post(url, json=payload)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            # Network layer never got a response. Retry-friendly.
            raise TransientTelegramError(f"telegram transport: {exc}") from exc
    finally:
        if owned_client:
            http.close()

    status = response.status_code
    if status == 200:
        _log.info("telegram.send.ok", chat_id=chat_id)
        return
    # Rate limiting and server-side blips are worth another try.
    if status == 429 or 500 <= status < 600:
        raise TransientTelegramError(f"telegram {status}: {response.text[:200]}")
    # Anything else (400 bad chat id, 401 revoked token) is not going
    # to fix itself; do not spend more attempts on it.
    raise PermanentTelegramError(f"telegram {status}: {response.text[:200]}")


__all__ = [
    "PermanentTelegramError",
    "TelegramError",
    "TransientTelegramError",
    "send_message",
]
