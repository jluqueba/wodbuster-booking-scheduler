"""Component tests for the /book-now manual booking route (US8.2).

Drives ``GET /book-now`` and ``POST /book-now`` end-to-end against a
real Postgres schema, a signed-in operator, a seeded cookie, and a
scripted WodBuster client. Covers:

- The form renders for an authenticated operator.
- POST for an in-window class with an available slot is granted and
  persists a ``booking_outcome`` row with ``rule_id IS NULL`` (US8.2,
  AS2 web-surface counterpart of CC-013).
- POST outside the reservation window is rejected with no ``inscribir``
  (booking) call and no persisted outcome (CC-010).
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.security.cipher import Cipher
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


def _seed_cookie(engine: Engine, operator_id: int) -> CookieStore:
    store = CookieStore(Cipher(os.urandom(32)))
    factory = sessionmaker(bind=engine)
    with factory() as session:
        store.save(session, operator_id, ".WBAuth-tok", validated_at=datetime.now(tz=UTC))
        session.commit()
    return store


class _FakeWodBusterClient:
    """Stub WodBuster client scripting ``load_class`` + ``inscribir``."""

    def __init__(
        self,
        *,
        load_response: LoadClassResponse | Exception | None = None,
        inscribir_response: BookingActionResponse | Exception | None = None,
    ) -> None:
        self._load_response = load_response
        self._inscribir_response = inscribir_response
        self.load_calls: list[dict[str, Any]] = []
        self.inscribir_calls: list[dict[str, Any]] = []

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        self.load_calls.append({"cookie": cookie_value, "ticks": ticks})
        if isinstance(self._load_response, Exception):
            raise self._load_response
        if self._load_response is None:
            raise AssertionError("fake: no load_class response scripted")
        return self._load_response

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
                                "TipoEstado": "Inscribible",
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


def _inscribir_ok() -> BookingActionResponse:
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="granted",
        raw_res="Ok",
        payload={"Res": "Ok", "Data": []},
    )


def _post_book_now(client: TestClient, *, book_date: str, book_time: str) -> Any:
    return client.post(
        "/book-now",
        data={
            "book_date": book_date,
            "book_time": book_time,
            "_csrf": client.cookies["wodbuster_csrf"],
        },
        headers=_csrf_headers(client),
    )


def test_book_now_form_renders(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GET /book-now renders the form for a signed-in operator."""
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.get("/book-now")
    assert resp.status_code == 200
    assert "book-now" in resp.text or "book_now" in resp.text or "/book-now" in resp.text


def test_book_now_grants_within_window(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """US8.2: an in-window class with an available slot is granted and
    the outcome is persisted with ``rule_id IS NULL``."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    store = _seed_cookie(postgres_engine, op_id)
    fake = _FakeWodBusterClient(
        load_response=_load_response_with("WOD", "18:30"),
        inscribir_response=_inscribir_ok(),
    )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        app.state.cookie_store = store
        app.state.wodbuster_client = fake
        resp = _post_book_now(client, book_date="2026-07-15", book_time="18:30")

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/book-now?")
    assert "flash_kind=info" in location
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


def test_book_now_window_closed_rejects_without_booking(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-010: booking outside the reservation window is rejected with
    no ``inscribir`` call and no persisted outcome."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    store = _seed_cookie(postgres_engine, op_id)
    fake = _FakeWodBusterClient(
        load_response=_load_response_with("WOD", "18:30", seconds_until_publication=3600.0),
        inscribir_response=_inscribir_ok(),
    )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        app.state.cookie_store = store
        app.state.wodbuster_client = fake
        resp = _post_book_now(client, book_date="2026-07-15", book_time="18:30")

    assert resp.status_code == 303
    location = resp.headers["location"]
    assert location.startswith("/book-now?")
    assert "flash_kind=warning" in location
    assert fake.inscribir_calls == []
    with postgres_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM booking_outcome WHERE operator_id = :op"),
            {"op": op_id},
        ).scalar_one()
    assert count == 0
