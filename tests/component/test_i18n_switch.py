"""Component tests for the language switch endpoint and picker."""

from __future__ import annotations

import re
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


_CSRF_PATTERN = re.compile(
    r'action="/settings/language"[\s\S]*?name="_csrf" value="([^"]+)"'
)


def _extract_csrf(html: str) -> str:
    match = _CSRF_PATTERN.search(html)
    assert match, "language picker CSRF token not found in HTML"
    return match.group(1)


def test_language_switch_writes_session_and_redirects(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del postgres_engine
    _, subject = seed_operator(display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        page = client.get("/")
        assert page.status_code == 200, page.text
        assert "Rules" in page.text  # English label present initially

        csrf_token = _extract_csrf(page.text)
        resp = client.post(
            "/settings/language",
            data={"lang": "es", "_csrf": csrf_token},
            headers={"referer": "http://testserver/"},
        )
        assert resp.status_code == 303, resp.text

        rendered = client.get("/").text
        assert "Reglas" in rendered
        assert "wb-lang-picker__btn--active" in rendered


def test_language_defaults_to_english_without_preference(
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
        assert "Rules" in body


def test_language_rejects_unknown_code(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del postgres_engine
    _, subject = seed_operator(display_name="Carol")
    app = app_factory()
    with _sign_in(app, subject, "Carol", monkeypatch) as client:
        page = client.get("/")
        csrf_token = _extract_csrf(page.text)

        resp = client.post(
            "/settings/language",
            data={"lang": "fr", "_csrf": csrf_token},
            headers={"referer": "http://testserver/"},
        )
        assert resp.status_code == 303
        rendered = client.get("/").text
        assert "Rules" in rendered  # fell back to English
