"""Component tests for the dashboard banner stack (US2.7).

Signs in as an operator, seeds an open alert row in the DB, hits
``GET /``, and asserts the banner partial renders the alert's
kind-specific heading and body. Also verifies that closed alerts
disappear from the stack (banner reflects DB truth).
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


def _sign_in(
    app: FastAPI,
    subject_id: str,
    display_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Drive the OAuth callback and return a logged-in :class:`TestClient`."""
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


def _open_alert(
    engine: Engine,
    *,
    operator_id: int,
    kind: str,
    payload: dict[str, Any] | None = None,
) -> int:
    """Insert an open alert row directly. Returns the row id."""
    import json

    now = datetime.now(tz=UTC)
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO alert "
                    "(operator_id, kind, payload, first_emitted_at, last_emitted_at) "
                    "VALUES (:op, :k, CAST(:p AS jsonb), :now, :now) "
                    "RETURNING id"
                ),
                {
                    "op": operator_id,
                    "k": kind,
                    "p": json.dumps(payload or {}),
                    "now": now,
                },
            ).scalar_one()
        )


def test_dashboard_renders_no_banner_stack_when_no_open_alerts(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'class="wb-banner-stack"' not in response.text


def test_dashboard_renders_cookie_expiring_banner(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    _open_alert(
        postgres_engine,
        operator_id=op_id,
        kind="cookie_expiring",
        payload={
            "kind": "cookie_expiring",
            "next_window_at": "2026-07-15T21:30:00+00:00",
            "projected_ttl_at": "2026-07-14T00:00:00+00:00",
        },
    )
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'class="wb-banner-stack"' in response.text
    assert 'data-alert-kind="cookie_expiring"' in response.text
    assert "Cookie expiring soon" in response.text
    assert "2026-07-15T21:30:00+00:00" in response.text


def test_closed_alerts_are_not_shown(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    alert_id = _open_alert(
        postgres_engine, operator_id=op_id, kind="cookie_invalid"
    )
    with postgres_engine.begin() as conn:
        conn.execute(
            text("UPDATE alert SET closed_at = NOW() WHERE id = :id"),
            {"id": alert_id},
        )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'data-alert-kind="cookie_invalid"' not in response.text


def test_dashboard_isolates_banners_by_operator(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_a, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    _op_b, _ = seed_operator(provider="microsoft", display_name="Bob")
    # Only Alice has an open alert. Bob must not see it on their dashboard.
    _open_alert(
        postgres_engine,
        operator_id=op_a,
        kind="cookie_expiring",
        payload={"kind": "cookie_expiring", "next_window_at": "2026-07-15T21:30+00:00"},
    )

    app = app_factory()
    # Sign in as Alice: alert visible.
    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        response = client.get("/")
    assert 'data-alert-kind="cookie_expiring"' in response.text

    # Sign in as Bob (needs a fresh sub): alert invisible.
    _op_c, subject_c = seed_operator(provider="microsoft", display_name="Bob2")
    with _sign_in(app, subject_c, "Bob2", monkeypatch) as client:
        response = client.get("/")
    assert 'data-alert-kind="cookie_expiring"' not in response.text
