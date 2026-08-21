"""OAuth callback lifecycle: signup, pending, rejected, active (ADR-0010).

- An identity NOT in ``federated_identity`` creates a pending signup
  (a ``pending`` ``operator_profile`` + its identity) and renders the
  "request received" page with status 200. No session is seated.
- A known ``pending`` identity sees the same pending page; a ``rejected``
  identity re-requests by signing in again (its status flips back to
  ``pending``). Neither seats a session.
- An ``active`` identity is seated and its OAuth email is captured.
- No page leaks operator-linked strings.

Approach: monkeypatch the Authlib client's ``authorize_access_token``
and ``get`` calls so the callback receives a fabricated identity
payload without contacting the real provider.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine


def _patch_callback(
    app: FastAPI,
    provider: str,
    user_info: dict[str, Any],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stub the Authlib client to return ``user_info`` from the callback."""
    client = app.state.oauth.create_client(provider)

    async def fake_authorize_access_token(_request: Any) -> dict[str, Any]:
        # OIDC providers put the decoded userinfo on the token.
        return {"userinfo": user_info, "access_token": "fake-token"}

    class _FakeResp:
        def json(self) -> dict[str, Any]:
            return user_info

    async def fake_get(*_args: Any, **_kwargs: Any) -> _FakeResp:
        return _FakeResp()

    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)
    monkeypatch.setattr(client, "get", fake_get)


@pytest.mark.parametrize(
    ("provider", "user_info"),
    [
        ("microsoft", {"sub": "unknown-ms-subject", "name": "Impostor MS"}),
        ("github", {"id": 999_999_999, "login": "impostor-gh", "name": "Impostor"}),
        ("google", {"sub": "unknown-google-subject", "name": "Impostor Google"}),
    ],
)
def test_callback_creates_pending_signup_for_unknown_identity(
    provider: str,
    user_info: dict[str, Any],
    app_factory: Callable[..., FastAPI],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = app_factory()
    _patch_callback(app, provider, user_info, monkeypatch)

    with TestClient(app, follow_redirects=False) as client:
        response = client.get(f"/auth/{provider}/callback?code=fake&state=fake")

    # A new identity lands on the pending page (ADR-0010), not a denial.
    assert response.status_code == 200
    body = response.text
    assert "Request received" in body
    for leak_candidate in (
        "Impostor MS",
        "impostor-gh",
        "Impostor Google",
        "unknown-ms-subject",
        "unknown-google-subject",
    ):
        assert leak_candidate not in body

    # A pending, non-admin profile plus its identity were created.
    with postgres_engine.connect() as conn:
        row = conn.execute(text("SELECT status, is_admin FROM operator_profile")).one()
        ident_count = conn.execute(text("SELECT COUNT(*) FROM federated_identity")).scalar_one()
    assert row.status == "pending"
    assert row.is_admin is False
    assert ident_count == 1

    # No session was seated: a follow-up GET / renders the anonymous landing.
    with TestClient(app, follow_redirects=False, cookies=response.cookies) as client:
        follow = client.get("/")
    assert follow.status_code == 200
    assert "Sign in with Microsoft" in follow.text


def test_pending_page_uses_the_login_language(
    app_factory: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A user who started on /es sees the pending page in Spanish.

    The callback URL has no /es prefix, so without restoring the language
    stored at login the page would fall back to English.
    """
    from starlette.responses import RedirectResponse

    app = app_factory()
    _patch_callback(app, "microsoft", {"sub": "unknown-es", "name": "X"}, monkeypatch)
    client_obj = app.state.oauth.create_client("microsoft")

    async def fake_authorize_redirect(_request: Any, _redirect_uri: str, **_kw: Any) -> Any:
        return RedirectResponse("https://provider/authorize", status_code=302)

    monkeypatch.setattr(client_obj, "authorize_redirect", fake_authorize_redirect)

    with TestClient(app, follow_redirects=False) as client:
        client.get("/es/auth/microsoft/login")  # stores oauth_lang_prefix="/es"
        response = client.get("/auth/microsoft/callback?code=fake&state=fake")

    assert response.status_code == 200
    assert "Solicitud recibida" in response.text
    assert "Request received" not in response.text


def test_allow_listed_identity_seats_session(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Positive-path sanity: an allow-listed identity gets a session."""
    operator_id, subject_id = seed_operator(provider="microsoft", display_name="Alice")

    app = app_factory()
    _patch_callback(
        app,
        "microsoft",
        {"sub": subject_id, "name": "Alice"},
        monkeypatch,
    )

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/auth/microsoft/callback?code=fake&state=fake")
        assert response.status_code == 302
        assert response.headers["location"] == "/"

        # Follow the redirect with the same cookie jar; the dashboard
        # must return 200 and identify the operator (visible only to
        # the authenticated user).
        follow = client.get("/")

    assert follow.status_code == 200
    # The dashboard renders the operator's display_name in the greeting
    # and carries a ``data-operator-id`` marker for future scoping tests.
    assert "Alice" in follow.text
    assert f'data-operator-id="{operator_id}"' in follow.text


def _set_status(engine: Engine, operator_id: int, status: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE operator_profile SET status = :s WHERE id = :i"),
            {"s": status, "i": operator_id},
        )


def test_pending_identity_shows_pending_page_without_session(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A known-but-pending identity sees the pending page and gets no session."""
    operator_id, subject_id = seed_operator(provider="microsoft", display_name="Pending Pat")
    _set_status(postgres_engine, operator_id, "pending")
    app = app_factory()
    _patch_callback(app, "microsoft", {"sub": subject_id, "name": "Pending Pat"}, monkeypatch)

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/auth/microsoft/callback?code=fake&state=fake")
        assert response.status_code == 200
        assert "Request received" in response.text
        follow = client.get("/")

    # No session was seated: the landing page renders, not the dashboard.
    assert follow.status_code == 200
    assert "Sign in with Microsoft" in follow.text


def test_rejected_identity_can_re_request_on_relogin(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rejected identity re-requests by signing in again (ADR-0010).

    Signing in flips the profile back to ``pending`` (recoverable from a
    mistaken rejection) and shows the pending page without seating a session.
    """
    operator_id, subject_id = seed_operator(provider="microsoft", display_name="Nope")
    _set_status(postgres_engine, operator_id, "rejected")
    app = app_factory()
    _patch_callback(app, "microsoft", {"sub": subject_id, "name": "Nope"}, monkeypatch)

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/auth/microsoft/callback?code=fake&state=fake")
        assert response.status_code == 200
        assert "Request received" in response.text
        follow = client.get("/")

    with postgres_engine.connect() as conn:
        status = conn.execute(
            text("SELECT status FROM operator_profile WHERE id = :i"),
            {"i": operator_id},
        ).scalar_one()
    assert status == "pending"
    # No session was seated: the landing page renders, not the dashboard.
    assert follow.status_code == 200
    assert "Sign in with Microsoft" in follow.text


def test_login_captures_oauth_email(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An active login stores the OAuth email on the profile (ADR-0010)."""
    operator_id, subject_id = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    _patch_callback(
        app,
        "microsoft",
        {"sub": subject_id, "name": "Alice", "email": "Alice@Example.COM"},
        monkeypatch,
    )

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/auth/microsoft/callback?code=fake&state=fake")
        assert response.status_code == 302

    with postgres_engine.connect() as conn:
        stored = conn.execute(
            text("SELECT email FROM operator_profile WHERE id = :i"),
            {"i": operator_id},
        ).scalar_one()
    assert stored == "alice@example.com"


def test_new_signup_notifies_admin_via_telegram(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new signup pings every Telegram-bound admin, best-effort (ADR-0010)."""
    admin_id, _ = seed_operator(provider="microsoft", display_name="Boss")
    with postgres_engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE operator_profile SET is_admin = true, telegram_chat_id = '999' "
                "WHERE id = :i"
            ),
            {"i": admin_id},
        )
    app = app_factory()
    sent: list[dict[str, str]] = []

    def fake_send(*, bot_token: str, chat_id: str, text: str, client: Any = None) -> None:
        sent.append({"bot_token": bot_token, "chat_id": chat_id, "text": text})

    monkeypatch.setattr("wodbuster_worker.auth.routes.send_message", fake_send)
    _patch_callback(app, "microsoft", {"sub": "brand-new", "name": "Newbie"}, monkeypatch)

    with TestClient(app, follow_redirects=False) as client:
        app.state.telegram_bot_token = "tok"
        response = client.get("/auth/microsoft/callback?code=fake&state=fake")

    assert response.status_code == 200
    assert len(sent) == 1
    assert sent[0]["chat_id"] == "999"
    assert "Newbie" in sent[0]["text"]
