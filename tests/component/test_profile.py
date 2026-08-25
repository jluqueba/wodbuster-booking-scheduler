"""Component tests for the profile view/edit page (User Profile T-UP-005).

Drives ``/profile`` end to end against a real Postgres schema. The
profile row is seeded together with the operator (``seed_operator``),
so these tests exercise the update path, validation flashes, and the
language enum guard.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine


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


def _csrf(client: TestClient) -> str:
    token = client.cookies.get("wodbuster_csrf")
    assert token, "expected wodbuster_csrf cookie after sign-in"
    return token


def _language(engine: Engine, operator_id: int) -> str:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT communication_language FROM operator_profile WHERE id = :id"),
            {"id": operator_id},
        ).scalar_one()


def test_profile_page_renders_current_values(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.get("/profile")
    assert resp.status_code == 200
    assert "Alice" in resp.text


def test_profile_page_shows_wodbuster_avatar(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The picture is the active gym's WodBuster photo (derived from its idu)."""
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.get("/profile")
    assert resp.status_code == 200
    assert "cdn.wodbuster.com/static/atletas" in resp.text


def test_profile_save_persists_fields_and_language(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.post(
            "/profile",
            data={
                "display_name": "Alice Cooper",
                "short_name": "Al",
                "communication_language": "es",
                "_csrf": _csrf(client),
            },
        )
    assert resp.status_code == 303
    assert "flash_kind=info" in resp.headers["location"]

    with postgres_engine.connect() as conn:
        row = conn.execute(
            text(
                "SELECT display_name, short_name, communication_language "
                "FROM operator_profile WHERE id = :id"
            ),
            {"id": op_id},
        ).one()
    assert row.display_name == "Alice Cooper"
    assert row.short_name == "Al"
    assert row.communication_language == "es"


def test_profile_save_persists_email_and_preferences(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.post(
            "/profile",
            data={
                "display_name": "Alice",
                "short_name": "",
                "communication_language": "en",
                "email": "alice@example.com",
                "email_bookings": "on",  # session_alerts omitted -> off
                "_csrf": _csrf(client),
            },
        )
    assert resp.status_code == 303
    assert "flash_kind=info" in resp.headers["location"]
    with postgres_engine.connect() as conn:
        row = conn.execute(
            text("SELECT email, email_preferences FROM operator_profile WHERE id = :id"),
            {"id": op_id},
        ).one()
    assert row.email == "alice@example.com"
    assert row.email_preferences == {"bookings": True, "session_alerts": False}


def test_profile_save_rejects_bad_email(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.post(
            "/profile",
            data={
                "display_name": "Alice",
                "short_name": "",
                "communication_language": "en",
                "email": "not-an-email",
                "_csrf": _csrf(client),
            },
        )
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]


def test_profile_save_rejects_empty_display_name(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.post(
            "/profile",
            data={
                "display_name": "   ",
                "short_name": "",
                "communication_language": "en",
                "_csrf": _csrf(client),
            },
        )
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]
    # The stored name is unchanged.
    with postgres_engine.connect() as conn:
        name = conn.execute(
            text("SELECT display_name FROM operator_profile WHERE id = :id"),
            {"id": op_id},
        ).scalar_one()
    assert name == "Alice"


def test_profile_save_rejects_unsupported_language(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.post(
            "/profile",
            data={
                "display_name": "Alice",
                "short_name": "",
                "communication_language": "fr",
                "_csrf": _csrf(client),
            },
        )
    assert resp.status_code == 303
    assert "flash_kind=error" in resp.headers["location"]
    # The enum guard left the stored language at its default.
    assert _language(postgres_engine, op_id) == "en"


def test_profile_requires_auth(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    client = TestClient(app, follow_redirects=False)
    resp = client.get("/profile")
    assert resp.status_code == 302
    assert "/auth/microsoft/login" in resp.headers["location"]


def test_nav_renders_account_menu_with_profile_and_logout(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The nav shows the avatar disclosure with Profile + Log out inside."""
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        resp = client.get("/")
    assert resp.status_code == 200
    body = resp.text
    # The disclosure container and its two actions render.
    assert "data-wb-usermenu" in body
    assert "/profile" in body
    assert "/auth/logout" in body
    # The signed-in name appears in the trigger/header.
    assert "Alice" in body


def _sign_in_with_email(
    app: FastAPI,
    subject_id: str,
    display_name: str,
    email: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Sign in with a provider payload that carries an email address."""
    client = app.state.oauth.create_client("microsoft")

    async def fake_authorize_access_token(_request: Any) -> dict[str, Any]:
        return {
            "userinfo": {"sub": subject_id, "name": display_name, "email": email},
            "access_token": "t",
        }

    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)
    tc = TestClient(app, follow_redirects=False)
    resp = tc.get("/auth/microsoft/callback?code=fake&state=fake")
    assert resp.status_code == 302, resp.text
    return tc


def _email(engine: Engine, operator_id: int) -> str | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT email FROM operator_profile WHERE id = :id"),
            {"id": operator_id},
        ).scalar_one()


def _set_email(engine: Engine, operator_id: int, email: str | None) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE operator_profile SET email = :email WHERE id = :id"),
            {"id": operator_id, "email": email},
        )


def test_login_does_not_overwrite_an_edited_email(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The provider address is a seed, not the source of truth.

    Writing it on every login silently reverted the address the operator
    chose, so notifications went back to the login mailbox.
    """
    operator_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    _set_email(postgres_engine, operator_id, "chosen@example.com")
    app = app_factory()

    with _sign_in_with_email(app, subject, "Alice", "login@example.com", monkeypatch):
        pass

    assert _email(postgres_engine, operator_id) == "chosen@example.com"


def test_login_fills_an_empty_email(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A profile with no address still gets one, which backfills old rows."""
    operator_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    _set_email(postgres_engine, operator_id, None)
    app = app_factory()

    with _sign_in_with_email(app, subject, "Alice", "login@example.com", monkeypatch):
        pass

    assert _email(postgres_engine, operator_id) == "login@example.com"
