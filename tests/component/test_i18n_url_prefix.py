"""Component tests for URL-prefix language routing (``/es/*`` vs default)."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine


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


def test_root_with_spanish_accept_language_redirects_to_es(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/", headers={"accept-language": "es-ES,es;q=0.9,en;q=0.8"})

    assert resp.status_code == 302
    assert resp.headers["location"] == "/es"


def test_root_with_english_accept_language_stays_on_root(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/", headers={"accept-language": "en-US,en;q=0.9"})

    assert resp.status_code == 200
    # English label is present; Spanish is not.
    assert "WodBuster Booking Scheduler" in resp.text or "Sign in with" in resp.text
    assert "Entrar con" not in resp.text


def test_es_landing_renders_spanish_content(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/es")

    assert resp.status_code == 200
    assert "Entrar con Microsoft" in resp.text
    assert "Entrar con Google" in resp.text
    # Language does not leak the English label alongside the Spanish one.
    assert "Sign in with Microsoft" not in resp.text


def test_es_landing_with_trailing_slash_matches_root(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/es/")

    assert resp.status_code == 200
    assert "Entrar con Microsoft" in resp.text


def test_es_landing_sign_in_links_keep_prefix(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        body = client.get("/es").text

    assert 'href="/es/auth/microsoft/login"' in body
    assert 'href="/es/auth/google/login"' in body


def test_es_protected_route_redirects_to_es_login(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        resp = client.get("/es/rules")

    assert resp.status_code == 302
    assert resp.headers["location"] == "/es/auth/microsoft/login"


def test_dashboard_nav_links_keep_es_prefix(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del postgres_engine
    _, subject = seed_operator(display_name="Ana")
    app = app_factory()
    with _sign_in(app, subject, "Ana", monkeypatch) as client:
        body = client.get("/es").text

    assert 'href="/es/rules"' in body
    assert 'href="/es/history"' in body
    assert 'href="/es/vacation"' in body
    assert 'href="/es/telegram"' in body
    # Spanish nav labels are present.
    assert "Reglas" in body
    assert "Historial" in body


def test_english_dashboard_nav_has_no_prefix(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del postgres_engine
    _, subject = seed_operator(display_name="Bob")
    app = app_factory()
    with _sign_in(app, subject, "Bob", monkeypatch) as client:
        body = client.get("/").text

    assert 'href="/rules"' in body
    assert 'href="/es/rules"' not in body
    assert "Rules" in body


def test_language_picker_removed_from_nav(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del postgres_engine
    _, subject = seed_operator(display_name="Carol")
    app = app_factory()
    with _sign_in(app, subject, "Carol", monkeypatch) as client:
        body = client.get("/").text

    assert "wb-lang-picker" not in body
    assert "/settings/language" not in body


def test_language_switch_endpoint_removed(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        resp = client.post("/settings/language", data={"lang": "es"})

    assert resp.status_code == 404
