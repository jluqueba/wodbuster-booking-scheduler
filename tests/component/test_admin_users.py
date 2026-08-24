"""Component tests for the admin user-management surface (ADR-0010).

An admin lists pending signups (approve / reject) and manages active users
(ban for a period, ban indefinitely, un-ban, delete). A non-admin cannot see
or reach any admin route (404). Bans take effect immediately: a banned login
is blocked and a banned open session is bounced. Admins cannot be banned or
deleted, and an admin cannot act on themselves.
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

from .conftest import confirm_messages


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


def _make_admin(engine: Engine, operator_id: int) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE operator_profile SET is_admin = true WHERE id = :i"),
            {"i": operator_id},
        )


def _set_status(engine: Engine, operator_id: int, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE operator_profile SET status = :s WHERE id = :i"),
            {"s": status, "i": operator_id},
        )


def _status(engine: Engine, operator_id: int) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT status FROM operator_profile WHERE id = :i"),
                {"i": operator_id},
            ).scalar_one()
        )


def _banned_until(engine: Engine, operator_id: int) -> datetime | None:
    with engine.connect() as conn:
        return conn.execute(
            text("SELECT banned_until FROM operator_profile WHERE id = :i"),
            {"i": operator_id},
        ).scalar_one_or_none()


def _exists(engine: Engine, operator_id: int) -> bool:
    with engine.connect() as conn:
        return (
            conn.execute(
                text("SELECT count(*) FROM operator_profile WHERE id = :i"),
                {"i": operator_id},
            ).scalar_one()
            == 1
        )


def _set_banned(engine: Engine, operator_id: int, when: datetime) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE operator_profile SET banned_until = :w WHERE id = :i"),
            {"w": when, "i": operator_id},
        )


def test_admin_lists_pending_and_active(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    pending_id, _ = seed_operator(provider="microsoft", display_name="Newcomer Nadia")
    _set_status(postgres_engine, pending_id, "pending")
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        resp = client.get("/admin/users")

    assert resp.status_code == 200
    assert "Newcomer Nadia" in resp.text  # pending
    assert "Boss" in resp.text  # active users section lists the admin


@pytest.mark.parametrize(
    ("prefix", "ban_message", "delete_message"),
    [
        (
            "",
            "Ban this user? They will lose access until the ban expires.",
            "Delete this user and all their data? This cannot be undone.",
        ),
        (
            "/es",
            "\u00bfBanear a este usuario? Perder\u00e1 el acceso hasta que expire el baneo.",
            "\u00bfEliminar este usuario y todos sus datos? No se puede deshacer.",
        ),
    ],
    ids=["en", "es"],
)
def test_admin_confirmations_carry_the_prompt(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
    ban_message: str,
    delete_message: str,
) -> None:
    """Ban and delete must still ask before they fire.

    The prompt travels as text in ``data-wb-confirm``, so the assertion
    is on the value a browser hands the listener after entity decoding.
    Both languages are rendered because only one of them carries an
    apostrophe.
    """
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    seed_operator(provider="microsoft", display_name="Regular Rita")
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        resp = client.get(f"{prefix}/admin/users")

    assert resp.status_code == 200
    assert confirm_messages(resp.text) == [ban_message, delete_message]


def test_non_admin_cannot_reach_admin_page(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _regular_id, subject = seed_operator(provider="microsoft", display_name="Regular")
    app = app_factory()

    with _sign_in(app, subject, "Regular", monkeypatch) as client:
        resp = client.get("/admin/users")

    assert resp.status_code == 404


def test_admin_approve_and_reject(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    a_id, _ = seed_operator(provider="microsoft", display_name="Pat")
    b_id, _ = seed_operator(provider="microsoft", display_name="Nope")
    _set_status(postgres_engine, a_id, "pending")
    _set_status(postgres_engine, b_id, "pending")
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        assert (
            client.post(f"/admin/users/{a_id}/approve", data={"_csrf": _csrf(client)}).status_code
            == 303
        )
        assert (
            client.post(f"/admin/users/{b_id}/reject", data={"_csrf": _csrf(client)}).status_code
            == 303
        )

    assert _status(postgres_engine, a_id) == "active"
    assert _status(postgres_engine, b_id) == "rejected"


def test_admin_approve_enqueues_account_email(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    pending_id, _ = seed_operator(provider="microsoft", display_name="Pat")
    _set_status(postgres_engine, pending_id, "pending")
    with postgres_engine.begin() as conn:
        conn.execute(
            text("UPDATE operator_profile SET email = 'pat@example.com' WHERE id = :id"),
            {"id": pending_id},
        )
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        resp = client.post(f"/admin/users/{pending_id}/approve", data={"_csrf": _csrf(client)})
        assert resp.status_code == 303

    with postgres_engine.connect() as conn:
        payload = conn.execute(
            text("SELECT payload FROM notification_outbox WHERE user_id = :id AND kind = 'email'"),
            {"id": pending_id},
        ).scalar_one()
    assert payload["kind"] == "account_approved"


def test_admin_ban_temporary_and_unban(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    user_id, _ = seed_operator(provider="microsoft", display_name="Regular")
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        ban = client.post(
            f"/admin/users/{user_id}/ban",
            data={"duration": "7d", "_csrf": _csrf(client)},
        )
        assert ban.status_code == 303
        banned_until = _banned_until(postgres_engine, user_id)
        assert banned_until is not None and banned_until > datetime.now(tz=UTC)

        unban = client.post(f"/admin/users/{user_id}/unban", data={"_csrf": _csrf(client)})
        assert unban.status_code == 303

    assert _banned_until(postgres_engine, user_id) is None


def test_admin_ban_indefinite(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    user_id, _ = seed_operator(provider="microsoft", display_name="Regular")
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        resp = client.post(
            f"/admin/users/{user_id}/ban",
            data={"duration": "indefinite", "_csrf": _csrf(client)},
        )
        assert resp.status_code == 303

    banned_until = _banned_until(postgres_engine, user_id)
    assert banned_until is not None and banned_until.year == 9999


def test_admin_delete_removes_user(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    user_id, _ = seed_operator(provider="microsoft", display_name="Regular")
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        resp = client.post(f"/admin/users/{user_id}/delete", data={"_csrf": _csrf(client)})
        assert resp.status_code == 303

    assert _exists(postgres_engine, user_id) is False


def test_admin_cannot_ban_or_delete_another_admin(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    other_admin_id, _ = seed_operator(provider="microsoft", display_name="Co-Admin")
    _make_admin(postgres_engine, other_admin_id)
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        client.post(
            f"/admin/users/{other_admin_id}/ban",
            data={"duration": "indefinite", "_csrf": _csrf(client)},
        )
        client.post(f"/admin/users/{other_admin_id}/delete", data={"_csrf": _csrf(client)})

    assert _banned_until(postgres_engine, other_admin_id) is None
    assert _exists(postgres_engine, other_admin_id) is True


def test_admin_cannot_delete_self(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        client.post(f"/admin/users/{admin_id}/delete", data={"_csrf": _csrf(client)})

    assert _exists(postgres_engine, admin_id) is True


def test_banned_login_shows_suspended_page(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A banned active user cannot sign in; they see the suspended page."""
    user_id, subject = seed_operator(provider="microsoft", display_name="Banned Bob")
    _set_banned(postgres_engine, user_id, datetime(2999, 1, 1, tzinfo=UTC))
    app = app_factory()
    client = app.state.oauth.create_client("microsoft")

    async def fake_token(_request: Any) -> dict[str, Any]:
        return {"userinfo": {"sub": subject, "name": "Banned Bob"}, "access_token": "t"}

    monkeypatch.setattr(client, "authorize_access_token", fake_token)
    with TestClient(app, follow_redirects=False) as tc:
        resp = tc.get("/auth/microsoft/callback?code=fake&state=fake")
        assert resp.status_code == 200
        assert "Access suspended" in resp.text
        follow = tc.get("/")
    # No session was seated.
    assert follow.status_code == 200
    assert "Sign in with Microsoft" in follow.text


def test_banned_open_session_is_bounced(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ban applied to an open session bounces the next protected request."""
    user_id, subject = seed_operator(provider="microsoft", display_name="Regular")
    app = app_factory()

    with _sign_in(app, subject, "Regular", monkeypatch) as client:
        assert client.get("/rules", follow_redirects=False).status_code == 200
        _set_banned(postgres_engine, user_id, datetime(2999, 1, 1, tzinfo=UTC))
        bounced = client.get("/rules", follow_redirects=False)

    assert bounced.status_code == 302
    assert bounced.headers["location"].endswith("/auth/suspended")


def test_admin_dashboard_shows_pending_banner(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    admin_id, admin_subject = seed_operator(provider="microsoft", display_name="Boss")
    _make_admin(postgres_engine, admin_id)
    pending_id, _ = seed_operator(provider="microsoft", display_name="Pat")
    _set_status(postgres_engine, pending_id, "pending")
    app = app_factory()

    with _sign_in(app, admin_subject, "Boss", monkeypatch) as client:
        resp = client.get("/", follow_redirects=False)

    assert resp.status_code == 200
    assert "/admin/users" in resp.text
