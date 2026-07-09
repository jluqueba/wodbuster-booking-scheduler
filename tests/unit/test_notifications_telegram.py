"""Unit tests for the Telegram Bot API sender (US2.2).

Uses ``httpx.MockTransport`` to intercept the outbound POST — no
network I/O. Verifies success, retry classification for the transient
(429, 5xx, network) versus permanent (4xx) buckets, and the exact
Bot API endpoint / JSON body the sender constructs.
"""

from __future__ import annotations

import httpx
import pytest

from wodbuster_worker.notifications.telegram import (
    PermanentTelegramError,
    TransientTelegramError,
    send_message,
)


def _client_with_handler(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_send_message_success_returns_none_and_calls_send_endpoint() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = request.content
        return httpx.Response(200, json={"ok": True})

    with _client_with_handler(handler) as client:
        send_message(
            bot_token="abc:token",
            chat_id="12345",
            text="hello",
            client=client,
        )

    assert captured["url"] == "https://api.telegram.org/botabc:token/sendMessage"
    assert b'"chat_id":"12345"' in captured["body"]  # type: ignore[operator]
    assert b'"text":"hello"' in captured["body"]  # type: ignore[operator]


@pytest.mark.parametrize("status", [429, 500, 502, 503])
def test_transient_status_codes_raise_transient(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="try later")

    with _client_with_handler(handler) as client, pytest.raises(TransientTelegramError):
        send_message(
            bot_token="t",
            chat_id="1",
            text="x",
            client=client,
        )


@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_permanent_status_codes_raise_permanent(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, text="nope")

    with _client_with_handler(handler) as client, pytest.raises(PermanentTelegramError):
        send_message(
            bot_token="t",
            chat_id="1",
            text="x",
            client=client,
        )


def test_network_error_raises_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    with _client_with_handler(handler) as client, pytest.raises(TransientTelegramError):
        send_message(
            bot_token="t",
            chat_id="1",
            text="x",
            client=client,
        )


def test_timeout_raises_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with _client_with_handler(handler) as client, pytest.raises(TransientTelegramError):
        send_message(
            bot_token="t",
            chat_id="1",
            text="x",
            client=client,
        )
