"""Component tests for automatic gym discovery and the shared cookie.

A single ``.WBAuth`` session authenticates every WodBuster gym the identity
can access, so gyms are never added by hand. These tests drive the two
surfaces that discover and store gyms:

- ``POST /cookie``: a valid paste is stored on every owned gym and triggers
  discovery of any new ones (best-effort: a selector failure still stores the
  cookie on the gyms already on file).
- login callback: signing in refreshes the accessible-gyms list using an
  already-stored cookie so new gyms appear in the switcher.

A fake selector + discovery client are injected on ``app.state`` so no live
WodBuster call occurs.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from wodbuster_worker.gyms.discovery import DiscoveredGym, GymSelectorError
from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.security.cipher import Cipher
from wodbuster_worker.security.cookie import Valid

_DISCOVERED_IDU = "bbbb1111cccc2222dddd3333eeee4444"


class _ScriptedValidator:
    """Fake CookieValidator that always returns a scripted Valid verdict."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def validate(self, cookie_value: str) -> Valid:
        self.calls.append(cookie_value)
        return Valid(probed_at=datetime(2026, 7, 7, 12, 0, tzinfo=UTC))


class _FakeDiscoveryClient:
    """Fake per-gym WodBuster client: returns a scripted idu."""

    def __init__(self, idu: str) -> None:
        self._idu = idu

    def discover_idu(self, cookie_value: str) -> str:
        return self._idu


def _wire_cookie_stack(app: FastAPI) -> CookieStore:
    """Install a real CookieStore + a scripted validator."""
    cipher = Cipher(b"k" * 32)
    store = CookieStore(cipher)
    app.state.cipher = cipher
    app.state.cookie_store = store
    app.state.cookie_validator = _ScriptedValidator()  # type: ignore[assignment]
    return store


def _wire_discovery(
    app: FastAPI,
    gyms: list[DiscoveredGym] | Exception,
) -> tuple[list[str], list[str]]:
    """Wire a fake selector + discovery factory; return (built, selector_calls)."""
    built: list[str] = []
    selector_calls: list[str] = []

    def factory(slug: str) -> _FakeDiscoveryClient:
        built.append(slug)
        return _FakeDiscoveryClient(_DISCOVERED_IDU)

    def selector(cookie_value: str) -> list[DiscoveredGym]:
        selector_calls.append(cookie_value)
        if isinstance(gyms, Exception):
            raise gyms
        return gyms

    app.state.gym_discovery_factory = factory
    app.state.gym_selector = selector
    return built, selector_calls


def _sign_in(
    app: FastAPI, subject_id: str, display_name: str, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    client = app.state.oauth.create_client("microsoft")

    async def fake_authorize_access_token(_request: Any) -> dict[str, Any]:
        return {"userinfo": {"sub": subject_id, "name": display_name}, "access_token": "t"}

    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)
    tc = TestClient(app, follow_redirects=False)
    resp = tc.get("/auth/microsoft/callback?code=fake&state=fake")
    assert resp.status_code == 302, resp.text
    return tc


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("wodbuster_csrf")
    assert token, "expected wodbuster_csrf cookie after sign-in"
    return {"X-CSRF-Token": token}


def _gym_id(engine: Engine, user_id: int, slug: str) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT id FROM gym_account WHERE user_id = :u AND gym_slug = :s"),
                {"u": user_id, "s": slug},
            ).scalar_one()
        )


def _cookie_ciphertext(engine: Engine, gym_account_id: int) -> bytes | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT cookie_ciphertext FROM cookie_credential WHERE gym_account_id = :g"),
            {"g": gym_account_id},
        ).scalar_one_or_none()


def _gym_count(engine: Engine, user_id: int) -> int:
    with engine.connect() as conn:
        return int(
            conn.execute(
                text("SELECT count(*) FROM gym_account WHERE user_id = :u"),
                {"u": user_id},
            ).scalar_one()
        )


def _store_cookie(app: FastAPI, engine: Engine, gym_account_id: int, slug: str, value: str) -> None:
    """Seed an encrypted cookie row directly, matching CookieStore's AAD."""
    associated_data = f"gym_account:{gym_account_id}:{slug}".encode()
    ciphertext, nonce = app.state.cipher.encrypt(
        value.encode("utf-8"), associated_data=associated_data
    )
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO cookie_credential "
                "(gym_account_id, cookie_ciphertext, cookie_nonce, "
                "last_validated_at, last_probe_status) "
                "VALUES (:g, :c, :n, now(), 'valid')"
            ),
            {"g": gym_account_id, "c": ciphertext, "n": nonce},
        )


def test_cookie_paste_stores_for_all_gyms_and_discovers_new(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    _wire_cookie_stack(app)
    built, selector_calls = _wire_discovery(
        app,
        [
            DiscoveredGym("antworktrainingcenter", "Antwork Training Center"),
            DiscoveredGym("elitefitness", "Elite Fitness"),
        ],
    )

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            "/cookie",
            data={"cookie_value": ".WBAuth-shared"},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 200
    assert "Cookie validated and stored for all your gyms." in response.text
    # Discovery reused the pasted cookie and only built the missing gym.
    assert selector_calls == [".WBAuth-shared"]
    assert built == ["elitefitness"]
    # Both gyms now exist and carry the cookie.
    antwork_id = _gym_id(postgres_engine, op_id, "antworktrainingcenter")
    elite_id = _gym_id(postgres_engine, op_id, "elitefitness")
    assert _cookie_ciphertext(postgres_engine, antwork_id) is not None
    assert _cookie_ciphertext(postgres_engine, elite_id) is not None


def test_cookie_paste_survives_selector_failure(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    _wire_cookie_stack(app)
    _wire_discovery(app, GymSelectorError("selector down"))

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            "/cookie",
            data={"cookie_value": ".WBAuth-shared"},
            headers=_csrf_headers(client),
        )

    # Selector failure is swallowed: the cookie is still stored on the
    # existing gym and no new gym is created.
    assert response.status_code == 200
    assert "Cookie validated and stored for all your gyms." in response.text
    assert _gym_count(postgres_engine, op_id) == 1
    antwork_id = _gym_id(postgres_engine, op_id, "antworktrainingcenter")
    assert _cookie_ciphertext(postgres_engine, antwork_id) is not None


def test_login_discovers_new_gyms_with_existing_cookie(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    _wire_cookie_stack(app)
    antwork_id = _gym_id(postgres_engine, op_id, "antworktrainingcenter")
    _store_cookie(app, postgres_engine, antwork_id, "antworktrainingcenter", ".WBAuth-shared")
    built, selector_calls = _wire_discovery(
        app,
        [
            DiscoveredGym("antworktrainingcenter", "Antwork Training Center"),
            DiscoveredGym("elitefitness", "Elite Fitness"),
        ],
    )

    # The OAuth callback runs discovery as a side effect. We do not enter the
    # TestClient lifespan (which would start the scheduler and probe the newly
    # discovered gym over the network) — the callback itself is enough.
    _sign_in(app, subject, "Alice", monkeypatch)

    assert selector_calls == [".WBAuth-shared"]
    assert built == ["elitefitness"]
    elite_id = _gym_id(postgres_engine, op_id, "elitefitness")
    assert _cookie_ciphertext(postgres_engine, elite_id) is not None


def test_login_without_cookie_does_not_discover(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    _wire_cookie_stack(app)
    _, selector_calls = _wire_discovery(
        app,
        [DiscoveredGym("elitefitness", "Elite Fitness")],
    )

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        # Login succeeds and no discovery runs without a stored cookie.
        assert client.get("/", follow_redirects=False).status_code == 200

    assert selector_calls == []
    assert _gym_count(postgres_engine, op_id) == 1
