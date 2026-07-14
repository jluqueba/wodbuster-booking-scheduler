"""Component tests for the Telegram webhook route (US9.8, US9.9).

Drives the ``POST /telegram/webhook/{secret}`` handler end-to-end
against real Postgres. Verifies:

- Path-secret guard (US9.9): wrong secret returns 404 without
  touching the operator profile.
- ``/start <token>``: valid token binds ``telegram_chat_id`` on the
  matching operator.
- ``/start`` with an unknown / expired token leaves state alone.
- Non-``/start`` messages leave state alone.

Uses ``TestClient`` and patches ``httpx.Client`` inside the webhook
module to a no-op so bot replies do not touch the network.
"""

from __future__ import annotations

from collections.abc import Callable
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from wodbuster_worker.notifications.telegram_bind import TelegramBindStore

_WEBHOOK_SECRET = "test-secret-abc"


def _override_after_lifespan(
    app: FastAPI, *, bind_store: TelegramBindStore, bot_token: str | None = None
) -> None:
    """Override the telegram-related app.state fields the lifespan seeded.

    The lifespan runs on ``TestClient.__enter__`` and writes
    ``telegram_webhook_secret = secrets.telegram_webhook_secret``
    (``None`` in the fabricated test :class:`Secrets`). Tests want a
    specific secret + a controlled bind store; overriding after
    lifespan is simpler than plumbing a Secrets override into the
    ``app_factory``.
    """
    app.state.telegram_webhook_secret = _WEBHOOK_SECRET
    app.state.telegram_bot_token = bot_token
    app.state.telegram_bind_store = bind_store


def _chat_id_for(engine: Engine, operator_id: int) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT telegram_chat_id FROM operator_profile WHERE id = :id"),
            {"id": operator_id},
        ).scalar_one_or_none()


def test_webhook_wrong_secret_returns_404(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = monkeypatch
    op_id, _ = seed_operator()
    app = app_factory()

    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore())
        response = client.post(
            "/telegram/webhook/wrong-secret",
            json={"message": {"chat": {"id": 999}, "text": "/start whatever"}},
        )

    assert response.status_code == 404
    assert _chat_id_for(postgres_engine, op_id) is None


def test_webhook_start_with_valid_token_binds_chat(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = monkeypatch
    op_id, _ = seed_operator()

    bind_store = TelegramBindStore()
    token = bind_store.issue(operator_id=op_id)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=bind_store)
        response = client.post(
            f"/telegram/webhook/{_WEBHOOK_SECRET}",
            json={
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "chat": {"id": 424242, "type": "private"},
                    "text": f"/start {token}",
                },
            },
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}
    assert _chat_id_for(postgres_engine, op_id) == "424242"


def test_webhook_start_reuse_token_leaves_state_alone(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Consumed tokens fail on the second try — a leaked token used
    twice cannot re-bind or steal the chat."""
    _ = monkeypatch
    op_id, _ = seed_operator()

    bind_store = TelegramBindStore()
    token = bind_store.issue(operator_id=op_id)
    # First consume happens here (simulating a first webhook call).
    assert bind_store.consume(token) == op_id

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=bind_store)
        response = client.post(
            f"/telegram/webhook/{_WEBHOOK_SECRET}",
            json={
                "message": {
                    "chat": {"id": 999999},
                    "text": f"/start {token}",
                },
            },
        )

    assert response.status_code == 200
    # Chat id remains unset — the stolen token did not bind.
    assert _chat_id_for(postgres_engine, op_id) is None


def test_webhook_unknown_command_leaves_state_alone(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _ = monkeypatch
    op_id, _ = seed_operator()
    app = app_factory()

    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore())
        response = client.post(
            f"/telegram/webhook/{_WEBHOOK_SECRET}",
            json={
                "message": {
                    "chat": {"id": 111},
                    "text": "hello bot",
                },
            },
        )

    assert response.status_code == 200
    assert _chat_id_for(postgres_engine, op_id) is None


def test_webhook_non_message_update_is_a_noop(
    app_factory: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Callback queries, edited channel posts, ... acknowledge silently."""
    _ = monkeypatch
    app = app_factory()

    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore())
        response = client.post(
            f"/telegram/webhook/{_WEBHOOK_SECRET}",
            json={"update_id": 5, "channel_post": {"chat": {"id": 1}}},
        )

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_webhook_start_reply_uses_bot_token_when_present(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webhook calls ``sendMessage`` via httpx when a bot token
    is on state — patch httpx.Client here so we can assert without
    hitting the network."""
    op_id, _ = seed_operator()
    bind_store = TelegramBindStore()
    token = bind_store.issue(operator_id=op_id)

    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=None)
    fake_client.post = MagicMock()
    monkeypatch.setattr(
        "wodbuster_worker.notifications.telegram_webhook.httpx.Client",
        MagicMock(return_value=fake_client),
    )

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=bind_store, bot_token="test-bot-token")
        client.post(
            f"/telegram/webhook/{_WEBHOOK_SECRET}",
            json={
                "message": {
                    "chat": {"id": 707070},
                    "text": f"/start {token}",
                },
            },
        )

    # Reply sent via the patched client.
    assert fake_client.post.called
    call = fake_client.post.call_args
    assert "sendMessage" in call.args[0]
    body = call.kwargs["json"]
    assert body["chat_id"] == "707070"
    assert "bound" in body["text"].lower()
