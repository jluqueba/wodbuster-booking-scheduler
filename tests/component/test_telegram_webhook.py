"""Component tests for the Telegram webhook route (US9.8, US9.9).

Drives the ``POST /telegram/webhook/{secret}`` handler end-to-end
against real Postgres. Verifies:

- Path-secret guard (US9.9): wrong secret returns 404 without
  touching the operator profile.
- ``/start <token>``: valid token binds ``telegram_chat_id`` on the
  matching operator.
- ``/start`` with an unknown / expired token leaves state alone.
- Non-``/start`` messages leave state alone.
- Command dispatcher (TG.2): ``/help``, ``/next``, ``/last``,
  ``/cancel``, ``/ack``, ``/bookclass`` route to their handlers;
  rule-mutation verbs are refused with an explanation (CC-009);
  stateful commands on an unbound chat leak no data (FR-031).

Uses ``TestClient`` and patches ``httpx.Client`` inside the webhook
module to a no-op so bot replies do not touch the network.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from wodbuster_worker.notifications.telegram_bind import TelegramBindStore
from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.security.cipher import Cipher
from wodbuster_worker.wodbuster_client.client import BookingActionResponse, LoadClassResponse

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


def _bind_chat(engine: Engine, operator_id: int, chat_id: str) -> None:
    """Bind ``chat_id`` to ``operator_id`` directly (skip the /start flow)."""
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE operator_profile SET telegram_chat_id = :cid WHERE id = :id"),
            {"cid": chat_id, "id": operator_id},
        )


def _capture_replies(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Patch ``httpx.Client`` in the webhook module and capture reply bodies.

    Returns a list that each ``sendMessage`` call appends its JSON body
    to, so tests can assert on the exact reply text without touching the
    network.
    """
    captured: list[dict[str, Any]] = []
    fake_client = MagicMock()
    fake_client.__enter__ = MagicMock(return_value=fake_client)
    fake_client.__exit__ = MagicMock(return_value=None)

    def _post(url: str, *, json: dict[str, Any] | None = None, **_: Any) -> MagicMock:
        captured.append(json or {})
        return MagicMock()

    fake_client.post = MagicMock(side_effect=_post)
    monkeypatch.setattr(
        "wodbuster_worker.notifications.telegram_webhook.httpx.Client",
        MagicMock(return_value=fake_client),
    )
    return captured


def _seed_booking(
    engine: Engine,
    *,
    operator_id: int,
    target_class: str = "WOD",
    target_slot: datetime | None = None,
    terminal_status: str = "granted",
) -> int:
    """Insert a booking outcome directly. Returns the row id."""
    if target_slot is None:
        target_slot = datetime.now(tz=UTC) + timedelta(days=3)
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO booking_outcome "
                    "(operator_id, target_class, target_slot, terminal_status) "
                    "VALUES (:op, :cls, :slot, :status) RETURNING id"
                ),
                {
                    "op": operator_id,
                    "cls": target_class,
                    "slot": target_slot,
                    "status": terminal_status,
                },
            ).scalar_one()
        )


def _seed_open_cookie_alert(engine: Engine, operator_id: int) -> int:
    """Insert an open (unacknowledged) cookie-expiring alert. Returns id."""
    now = datetime.now(tz=UTC)
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO alert "
                    "(operator_id, kind, payload, first_emitted_at, last_emitted_at) "
                    "VALUES (:op, 'cookie_expiring', CAST(:p AS jsonb), :now, :now) "
                    "RETURNING id"
                ),
                {"op": operator_id, "p": json.dumps({"kind": "cookie_expiring"}), "now": now},
            ).scalar_one()
        )


class _FakeWodBusterClient:
    """Stub WodBuster client scripting ``load_class`` + ``borrar`` + ``inscribir``."""

    def __init__(
        self,
        *,
        load_response: LoadClassResponse | Exception | None = None,
        borrar_response: BookingActionResponse | Exception | None = None,
        inscribir_response: BookingActionResponse | Exception | None = None,
    ) -> None:
        self._load_response = load_response
        self._borrar_response = borrar_response
        self._inscribir_response = inscribir_response
        self.load_calls: list[dict[str, Any]] = []
        self.borrar_calls: list[dict[str, Any]] = []
        self.inscribir_calls: list[dict[str, Any]] = []

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        self.load_calls.append({"cookie": cookie_value, "ticks": ticks})
        if isinstance(self._load_response, Exception):
            raise self._load_response
        if self._load_response is None:
            raise AssertionError("fake: no load_class response scripted")
        return self._load_response

    def borrar(
        self, cookie_value: str, *, class_id: str | int, ticks: int
    ) -> BookingActionResponse:
        self.borrar_calls.append({"cookie": cookie_value, "class_id": class_id, "ticks": ticks})
        if isinstance(self._borrar_response, Exception):
            raise self._borrar_response
        if self._borrar_response is None:
            raise AssertionError("fake: no borrar response scripted")
        return self._borrar_response

    def inscribir(
        self, cookie_value: str, *, class_id: str | int, ticks: int
    ) -> BookingActionResponse:
        self.inscribir_calls.append({"cookie": cookie_value, "class_id": class_id, "ticks": ticks})
        if isinstance(self._inscribir_response, Exception):
            raise self._inscribir_response
        if self._inscribir_response is None:
            raise AssertionError("fake: no inscribir response scripted")
        return self._inscribir_response


def _load_response_with(
    class_type: str, class_time: str, *, seconds_until_publication: float = -100.0
) -> LoadClassResponse:
    return LoadClassResponse(
        status_code=200,
        latency_ms=10.0,
        payload={
            "Data": [
                {
                    "Hora": f"{class_time}:00",
                    "Valores": [
                        {
                            "Valor": {
                                "Id": 45654,
                                "Nombre": class_type,
                                "HoraComienzo": f"{class_time}:00",
                                "TipoEstado": "Borrable",
                                "Plazas": 16,
                                "AtletasEnListaDeEspera": 0,
                            }
                        }
                    ],
                }
            ],
            "SegundosHastaPublicacion": seconds_until_publication,
        },
    )


def _borrar_ok() -> BookingActionResponse:
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="granted",
        raw_res="Ok",
        payload={"Res": "Ok"},
    )


def _inscribir_ok() -> BookingActionResponse:
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="granted",
        raw_res="Ok",
        payload={"Res": "Ok", "Data": []},
    )


def _seed_cookie(engine: Engine, operator_id: int) -> CookieStore:
    """Persist a validated cookie for ``operator_id`` and return the store."""
    store = CookieStore(Cipher(os.urandom(32)))
    factory = sessionmaker(bind=engine)
    with factory() as session:
        store.save(session, operator_id, ".WBAuth-tok", validated_at=datetime.now(tz=UTC))
        session.commit()
    return store


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


# ---------------------------------------------------------------------------
# Command dispatcher (TG.2)
# ---------------------------------------------------------------------------


def _post_command(client: TestClient, *, chat_id: int, text_body: str) -> None:
    client.post(
        f"/telegram/webhook/{_WEBHOOK_SECRET}",
        json={"message": {"chat": {"id": chat_id}, "text": text_body}},
    )


def test_help_lists_supported_commands(
    app_factory: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _capture_replies(monkeypatch)
    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        _post_command(client, chat_id=100, text_body="/help")

    assert replies, "expected a reply"
    body = replies[-1]["text"]
    for verb in ("/cancel", "/next", "/last", "/ack", "/bookclass"):
        assert verb in body


def test_rule_mutation_command_is_refused_with_explanation(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-009: rule verbs are web-UI only. The bot explains the refusal
    and changes no rule state."""
    op_id, _ = seed_operator()
    _bind_chat(postgres_engine, op_id, "555")
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        _post_command(client, chat_id=555, text_body="/deleterule 3")

    assert replies
    assert "web ui" in replies[-1]["text"].lower()
    # No rule was created by the attempt.
    with postgres_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM scheduler_rule WHERE operator_id = :op"),
            {"op": op_id},
        ).scalar_one()
    assert count == 0


def test_unknown_command_gets_polite_nudge(
    app_factory: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    replies = _capture_replies(monkeypatch)
    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        _post_command(client, chat_id=100, text_body="/frobnicate")

    assert replies
    assert "unknown command" in replies[-1]["text"].lower()


def test_next_on_unbound_chat_leaks_no_data(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-031: a stateful command from an unbound chat is refused
    without surfacing any operator data."""
    op_id, _ = seed_operator()
    _seed_booking(postgres_engine, operator_id=op_id, target_class="SecretWOD")
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        _post_command(client, chat_id=999, text_body="/next")

    assert replies
    body = replies[-1]["text"]
    assert "not bound" in body.lower()
    assert "SecretWOD" not in body


def test_last_reports_most_recent_outcome(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, _ = seed_operator()
    _bind_chat(postgres_engine, op_id, "424242")
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="LastWOD",
        terminal_status="granted",
    )
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        _post_command(client, chat_id=424242, text_body="/last")

    assert replies
    body = replies[-1]["text"]
    assert "LastWOD" in body
    assert "granted" in body
    assert "#" in body


def test_next_lists_upcoming_granted_booking(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, _ = seed_operator()
    _bind_chat(postgres_engine, op_id, "424242")
    booking_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="UpcomingWOD",
        target_slot=datetime.now(tz=UTC) + timedelta(days=2),
        terminal_status="granted",
    )
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        _post_command(client, chat_id=424242, text_body="/next")

    assert replies
    body = replies[-1]["text"]
    assert "UpcomingWOD" in body
    # Granted (cancellable) slots surface the id /cancel needs.
    assert f"#{booking_id}" in body


def test_cancel_flips_booking_and_confirms(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """US6.3: /cancel <id> cancels the booking via WodBuster and the
    bot confirms."""
    op_id, _ = seed_operator()
    _bind_chat(postgres_engine, op_id, "424242")
    booking_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
    )
    store = _seed_cookie(postgres_engine, op_id)
    fake = _FakeWodBusterClient(
        load_response=_load_response_with("WOD", "21:30"),
        borrar_response=_borrar_ok(),
    )
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        app.state.cookie_store = store
        app.state.wodbuster_client = fake
        _post_command(client, chat_id=424242, text_body=f"/cancel {booking_id}")

    assert replies
    assert f"#{booking_id}" in replies[-1]["text"]
    assert len(fake.borrar_calls) == 1
    with postgres_engine.connect() as conn:
        status = conn.execute(
            text("SELECT terminal_status FROM booking_outcome WHERE id = :id"),
            {"id": booking_id},
        ).scalar_one()
    assert status == "cancelled"


def test_cancel_already_cancelled_is_idempotent(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-015: a second /cancel is a no-op — no WodBuster call issued."""
    op_id, _ = seed_operator()
    _bind_chat(postgres_engine, op_id, "424242")
    booking_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        terminal_status="cancelled",
    )
    store = _seed_cookie(postgres_engine, op_id)
    fake = _FakeWodBusterClient()  # no responses scripted → raises if called
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        app.state.cookie_store = store
        app.state.wodbuster_client = fake
        _post_command(client, chat_id=424242, text_body=f"/cancel {booking_id}")

    assert replies
    assert "already cancelled" in replies[-1]["text"].lower()
    assert fake.borrar_calls == []
    assert fake.load_calls == []


def test_ack_acknowledges_open_cookie_alert(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """TG.5: /ack sets ``acknowledged_at`` on the operator's open
    cookie-expiring alert, and only that operator's alert."""
    op_id, _ = seed_operator(provider="microsoft", display_name="Alice")
    other_id, _ = seed_operator(provider="google", display_name="Bob")
    _bind_chat(postgres_engine, op_id, "424242")
    alert_id = _seed_open_cookie_alert(postgres_engine, op_id)
    other_alert_id = _seed_open_cookie_alert(postgres_engine, other_id)
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        _post_command(client, chat_id=424242, text_body="/ack")

    assert replies
    assert "acknowledged" in replies[-1]["text"].lower()
    with postgres_engine.connect() as conn:
        mine = conn.execute(
            text("SELECT acknowledged_at FROM alert WHERE id = :id"),
            {"id": alert_id},
        ).scalar_one()
        theirs = conn.execute(
            text("SELECT acknowledged_at FROM alert WHERE id = :id"),
            {"id": other_alert_id},
        ).scalar_one()
    assert mine is not None
    assert theirs is None


def test_ack_with_no_open_alert_reports_nothing_to_do(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, _ = seed_operator()
    _bind_chat(postgres_engine, op_id, "424242")
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        _post_command(client, chat_id=424242, text_body="/ack")

    assert replies
    assert "no open cookie-expiring" in replies[-1]["text"].lower()


def test_bookclass_books_within_window(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """US8.3 / CC-013: /bookclass for an in-window class with an
    available slot is granted on both surfaces (booking_outcome row
    with rule_id NULL + a notification signal)."""
    op_id, _ = seed_operator()
    _bind_chat(postgres_engine, op_id, "424242")
    store = _seed_cookie(postgres_engine, op_id)
    fake = _FakeWodBusterClient(
        load_response=_load_response_with("WOD", "18:30"),
        inscribir_response=_inscribir_ok(),
    )
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        app.state.cookie_store = store
        app.state.wodbuster_client = fake
        _post_command(client, chat_id=424242, text_body="/bookclass 2026-07-15 18:30")

    assert replies
    body = replies[-1]["text"].lower()
    assert "booked" in body
    assert "wod" in body
    assert len(fake.inscribir_calls) == 1
    with postgres_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT terminal_status, rule_id FROM booking_outcome "
                "WHERE operator_id = :op ORDER BY id DESC LIMIT 1"
            ),
            {"op": op_id},
        ).one()
    assert row.terminal_status == "granted"
    assert row.rule_id is None


def test_bookclass_window_closed_rejects_without_booking(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-010: /bookclass outside the reservation window is rejected
    with no ``inscribir`` (booking) call."""
    op_id, _ = seed_operator()
    _bind_chat(postgres_engine, op_id, "424242")
    store = _seed_cookie(postgres_engine, op_id)
    fake = _FakeWodBusterClient(
        load_response=_load_response_with("WOD", "18:30", seconds_until_publication=3600.0),
        inscribir_response=_inscribir_ok(),
    )
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        app.state.cookie_store = store
        app.state.wodbuster_client = fake
        _post_command(client, chat_id=424242, text_body="/bookclass 2026-07-15 18:30")

    assert replies
    assert "open for booking" in replies[-1]["text"].lower()
    # The mutating booking call must NOT fire while the window is closed.
    assert fake.inscribir_calls == []
    with postgres_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM booking_outcome WHERE operator_id = :op"),
            {"op": op_id},
        ).scalar_one()
    assert count == 0


def test_bookclass_rejects_malformed_arguments(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, _ = seed_operator()
    _bind_chat(postgres_engine, op_id, "424242")
    replies = _capture_replies(monkeypatch)

    app = app_factory()
    with TestClient(app) as client:
        _override_after_lifespan(app, bind_store=TelegramBindStore(), bot_token="tok")
        _post_command(client, chat_id=424242, text_body="/bookclass not-a-date")

    assert replies
    assert "usage" in replies[-1]["text"].lower()
