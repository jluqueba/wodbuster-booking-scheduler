"""US9.T3 — OAuth callback denies identities not on the allow-list.

Verifies AS3 / FR-030:

- The OAuth callback with an identity NOT present in
  ``federated_identity`` renders the denial page with status 403.
- No ``operator_profile`` row is created.
- No session is established (no ``operator_id`` in the session cookie).
- The response body carries the generic ``denied.html`` template and
  no operator-linked strings.

Approach: monkeypatch the Authlib client's ``authorize_access_token``
and ``get`` calls so the callback receives a fabricated identity
payload without contacting the real provider. Each provider is
tested to exercise the per-provider :func:`extract_identity` branch.
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
def test_callback_denies_non_allow_listed_identity(
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

    assert response.status_code == 403
    body = response.text
    # Static template markers present. No impostor identity leaks.
    assert "Access denied" in body
    for leak_candidate in (
        "Impostor MS",
        "impostor-gh",
        "Impostor Google",
        "unknown-ms-subject",
        "unknown-google-subject",
    ):
        assert leak_candidate not in body

    # No operator_profile was created as a side effect.
    with postgres_engine.connect() as conn:
        op_count = conn.execute(
            text("SELECT COUNT(*) FROM operator_profile")
        ).scalar_one()
        ident_count = conn.execute(
            text("SELECT COUNT(*) FROM federated_identity")
        ).scalar_one()
    assert op_count == 0
    assert ident_count == 0

    # No session cookie set (or set but empty). If Starlette flushed
    # a session cookie, it must not contain an operator_id — the
    # simplest check is that a follow-up GET / still renders the
    # anonymous landing page (200) with no operator data. If the
    # session had leaked, the branch would flip to the dashboard.
    with TestClient(app, follow_redirects=False, cookies=response.cookies) as client:
        follow = client.get("/")
    assert follow.status_code == 200
    assert "Sign in with Microsoft" in follow.text


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
        # must return 200 and mention the operator_id (visible only to
        # the authenticated user).
        follow = client.get("/")

    assert follow.status_code == 200
    assert f"<code>{operator_id}</code>" in follow.text
