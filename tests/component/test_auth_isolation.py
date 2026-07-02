"""US9.T2 — cross-operator isolation on protected routes.

Verifies CC-012: an operator authenticated as A cannot read or mutate
another operator's resources. Only ``/`` exists in US-009 core; the
per-resource routes (``/rules/{id}``, ``/history``) come with later
user stories, so those subtests are marked ``xfail(strict=False)``
with a reference to the task ID that will implement them.

Approach: seed two operators (A and B), sign the client in as A via
the OAuth callback stub, and confirm that:

1. The dashboard renders operator A's ID but never operator B's.
2. Attempts to reach operator-B-scoped resources return 404/403 with
   an empty body. Left as xfail placeholders for now.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


def _sign_in(
    app: FastAPI,
    provider: str,
    subject_id: str,
    display_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
    """Drive the OAuth callback and return a logged-in TestClient."""
    client = app.state.oauth.create_client(provider)

    async def fake_authorize_access_token(_request: Any) -> dict[str, Any]:
        return {
            "userinfo": {"sub": subject_id, "name": display_name},
            "access_token": "fake-token",
        }

    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)

    test_client = TestClient(app, follow_redirects=False)
    resp = test_client.get(f"/auth/{provider}/callback?code=fake&state=fake")
    assert resp.status_code == 302, resp.text
    return test_client


def test_dashboard_shows_only_own_operator_id(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Operator A sees their own operator_id; B's data does not appear."""
    op_a_id, subject_a = seed_operator(
        provider="microsoft", display_name="Alice"
    )
    op_b_id, _subject_b = seed_operator(
        provider="microsoft", display_name="Bob-Doe-42"
    )
    assert op_a_id != op_b_id

    app = app_factory()
    with _sign_in(app, "microsoft", subject_a, "Alice", monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.text
    assert f"<code>{op_a_id}</code>" in body
    # Neither Bob's operator_id nor his display name appears.
    assert f"<code>{op_b_id}</code>" not in body
    assert "Bob-Doe-42" not in body


@pytest.mark.xfail(strict=False, reason="US5.2 /rules/{id} not yet implemented")
def test_rules_route_denies_cross_operator_read(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # pragma: no cover - placeholder
    """Placeholder for US5.2: GET /rules/{other_op_id} → 404 / 403."""
    _, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    seed_operator(provider="microsoft", display_name="Bob")

    app = app_factory()
    with _sign_in(app, "microsoft", subject_a, "Alice", monkeypatch) as client:
        response = client.get("/rules/999999")

    assert response.status_code in (403, 404)


@pytest.mark.xfail(strict=False, reason="US5.2 /rules/{id} POST not yet implemented")
def test_rules_route_denies_cross_operator_mutation(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # pragma: no cover - placeholder
    """Placeholder for US5.2: POST /rules/{other_op_id} → 404 / 403."""
    _, subject_a = seed_operator(provider="microsoft", display_name="Alice")

    app = app_factory()
    with _sign_in(app, "microsoft", subject_a, "Alice", monkeypatch) as client:
        response = client.post("/rules/999999", json={})

    assert response.status_code in (403, 404)


@pytest.mark.xfail(strict=False, reason="H.1 GET /history not yet implemented")
def test_history_route_scopes_to_operator(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:  # pragma: no cover - placeholder
    """Placeholder for H.1: GET /history returns only own outcomes."""
    _, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    seed_operator(provider="microsoft", display_name="Bob-Doe-42")

    app = app_factory()
    with _sign_in(app, "microsoft", subject_a, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert "Bob-Doe-42" not in response.text
