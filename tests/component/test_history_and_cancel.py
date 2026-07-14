"""Component tests for /history + /bookings/{id}/cancel (US6.2, H.1 lite).

Covers:

- Empty state: operator with no bookings sees the "no bookings yet"
  copy.
- Row rendering: an operator sees their own bookings, with a Cancel
  button on granted+future rows only.
- Isolation (CC-012): Alice's history never shows Bob's outcomes.
- Cancel happy path: POST /bookings/{id}/cancel invokes borrar,
  flips the row to ``cancelled``, and enqueues a banner outbox.
- Cancel idempotency (CC-015): a second POST is a no-op.
- Cancel cross-operator: POST for a row owned by someone else 404s.
- Cancel with no wodbuster client wired: friendly error flash.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from wodbuster_worker.persistence.models import BookingOutcome, NotificationOutbox
from wodbuster_worker.wodbuster_client.client import BookingActionResponse, LoadClassResponse


def _sign_in(
    app: FastAPI,
    subject_id: str,
    display_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    client = app.state.oauth.create_client("microsoft")

    async def fake_authorize_access_token(_request: Any) -> dict[str, Any]:
        return {
            "userinfo": {"sub": subject_id, "name": display_name},
            "access_token": "fake-token",
        }

    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)
    tc = TestClient(app, follow_redirects=False)
    resp = tc.get("/auth/microsoft/callback?code=fake&state=fake")
    assert resp.status_code == 302, resp.text
    return tc


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("wodbuster_csrf")
    assert token, "expected wodbuster_csrf cookie after sign-in"
    return {"X-CSRF-Token": token}


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


class _FakeWodBusterClient:
    """Stub WodBuster client that scripts load_class + borrar responses."""

    def __init__(
        self,
        *,
        load_response: LoadClassResponse | Exception | None = None,
        borrar_response: BookingActionResponse | Exception | None = None,
    ) -> None:
        self._load_response = load_response
        self._borrar_response = borrar_response
        self.load_calls: list[dict[str, Any]] = []
        self.borrar_calls: list[dict[str, Any]] = []

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


def _load_response_with(class_type: str, class_time: str) -> LoadClassResponse:
    """Build a LoadClass payload containing one matching class instance."""
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
            "SegundosHastaPublicacion": -100.0,
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


# ---------------------------------------------------------------------------
# GET /history
# ---------------------------------------------------------------------------


def test_history_empty_state(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert "No bookings yet" in response.text


def test_history_lists_own_bookings_with_cancel_button_on_granted_future(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    granted_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        target_slot=datetime.now(tz=UTC) + timedelta(days=3),
        terminal_status="granted",
    )
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="Halterofilia",
        target_slot=datetime.now(tz=UTC) - timedelta(days=1),  # past
        terminal_status="granted",
    )
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="Cross Training",
        terminal_status="full",
    )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    # All three rows visible.
    assert "WOD" in response.text
    assert "Halterofilia" in response.text
    assert "Cross Training" in response.text
    # Cancel button only on the granted+future row.
    assert f'action="/bookings/{granted_id}/cancel"' in response.text
    # Past-granted and full rows do NOT get a cancel form.
    assert response.text.count("/cancel") == 1


def test_history_isolates_by_operator(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_a, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    op_b, _ = seed_operator(provider="microsoft", display_name="Bob")
    _seed_booking(postgres_engine, operator_id=op_b, target_class="BobsSecretClass")

    app = app_factory()
    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert "BobsSecretClass" not in response.text
    # Alice has no bookings → empty state.
    assert "No bookings yet" in response.text
    _ = op_a  # unused but binds the fixture return


def test_history_unauthenticated_redirects_to_login(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/history")
    assert response.status_code == 302
    assert "/auth/" in response.headers["location"]


# ---------------------------------------------------------------------------
# POST /bookings/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_granted_booking_flips_row_and_enqueues_outbox(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    booking_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
    )

    fake_client = _FakeWodBusterClient(
        load_response=_load_response_with("WOD", "21:30"),
        borrar_response=_borrar_ok(),
    )

    app = app_factory()
    # Cookie stack + fake client are seeded AFTER sign-in (which
    # triggers the lifespan) so the lifespan does not overwrite them.
    import os

    from wodbuster_worker.persistence.cookie_store import CookieStore
    from wodbuster_worker.security.cipher import Cipher

    cipher = Cipher(os.urandom(32))
    store = CookieStore(cipher)
    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        store.save(session, op_id, ".WBAuth-tok", validated_at=datetime.now(tz=UTC))
        session.commit()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        app.state.cookie_store = store
        app.state.wodbuster_client = fake_client
        response = client.post(
            f"/bookings/{booking_id}/cancel",
            data={"_csrf": client.cookies["wodbuster_csrf"]},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/history?")
    assert "Booking+cancelled" in response.headers["location"]

    # WodBuster was called with the resolved class id.
    assert len(fake_client.load_calls) == 1
    assert len(fake_client.borrar_calls) == 1
    assert fake_client.borrar_calls[0]["class_id"] == 45654

    # Row now marked cancelled with a paired outbox row.
    with factory() as session:
        row = session.get(BookingOutcome, booking_id)
        assert row is not None
        assert row.terminal_status == "cancelled"
        outbox = session.query(NotificationOutbox).filter_by(operator_id=op_id).all()
        # At least one banner row; the cancel path enqueues one banner
        # (Telegram only when chat_id is set — Alice has none in this test).
        kinds = [row.kind for row in outbox]
        assert "banner" in kinds


def test_cancel_already_cancelled_is_idempotent(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-015: a second cancel is a no-op — no WodBuster call issued."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    booking_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        terminal_status="cancelled",
    )

    fake_client = _FakeWodBusterClient()
    app = app_factory()
    import os

    from wodbuster_worker.persistence.cookie_store import CookieStore
    from wodbuster_worker.security.cipher import Cipher

    cipher = Cipher(os.urandom(32))
    store = CookieStore(cipher)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        app.state.cookie_store = store
        app.state.wodbuster_client = fake_client
        response = client.post(
            f"/bookings/{booking_id}/cancel",
            data={"_csrf": client.cookies["wodbuster_csrf"]},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    assert "Already+cancelled" in response.headers["location"]
    assert fake_client.load_calls == []
    assert fake_client.borrar_calls == []


def test_cancel_cross_operator_returns_404(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _op_a, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    op_b, _ = seed_operator(provider="microsoft", display_name="Bob")
    bob_booking = _seed_booking(postgres_engine, operator_id=op_b, target_class="WOD")

    fake_client = _FakeWodBusterClient()
    app = app_factory()
    import os

    from wodbuster_worker.persistence.cookie_store import CookieStore
    from wodbuster_worker.security.cipher import Cipher

    cipher = Cipher(os.urandom(32))

    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        app.state.cookie_store = CookieStore(cipher)
        app.state.wodbuster_client = fake_client
        response = client.post(
            f"/bookings/{bob_booking}/cancel",
            data={"_csrf": client.cookies["wodbuster_csrf"]},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 404
    assert fake_client.load_calls == []


def test_cancel_without_wodbuster_stack_returns_friendly_error(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    booking_id = _seed_booking(postgres_engine, operator_id=op_id)

    app = app_factory()
    # Deliberately do NOT wire wodbuster_client / cookie_store.
    assert app.state.wodbuster_client is None

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            f"/bookings/{booking_id}/cancel",
            data={"_csrf": client.cookies["wodbuster_csrf"]},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_cancel_without_csrf_is_forbidden(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    booking_id = _seed_booking(postgres_engine, operator_id=op_id)
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(f"/bookings/{booking_id}/cancel")

    assert response.status_code == 403
