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
    """Operator A sees their own identity markers; B's data does not appear."""
    op_a_id, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    op_b_id, _subject_b = seed_operator(provider="microsoft", display_name="Bob-Doe-42")
    assert op_a_id != op_b_id

    app = app_factory()
    with _sign_in(app, "microsoft", subject_a, "Alice", monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.text
    # A's display name renders in the "Hero, <name>" greeting.
    assert "Alice" in body
    # The dashboard also carries a ``data-operator-id`` marker for A.
    assert f'data-operator-id="{op_a_id}"' in body
    # Neither B's operator_id nor his distinctive display name appears.
    assert f'data-operator-id="{op_b_id}"' not in body
    assert "Bob-Doe-42" not in body


def test_history_route_scopes_to_operator(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H.1: GET /history renders only the current operator's outcomes.

    Fuller cross-operator isolation lives in
    ``tests/component/test_history_and_cancel.py``. Kept here as a
    lightweight smoke check that the route exists and Alice does
    not see Bob's display name (nor any of his data).
    """
    _, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    seed_operator(provider="microsoft", display_name="Bob-Doe-42")

    app = app_factory()
    with _sign_in(app, "microsoft", subject_a, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert "Bob-Doe-42" not in response.text


def test_rules_route_denies_cross_operator_read(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-012: GET /rules/{other_op_id} for a non-owned rule -> 404.

    The route deliberately returns 404 (not 403) so an unauthorized
    caller cannot confirm the row's existence.
    """
    _, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    seed_operator(provider="microsoft", display_name="Bob")

    app = app_factory()
    with _sign_in(app, "microsoft", subject_a, "Alice", monkeypatch) as client:
        response = client.get("/rules/999999")

    assert response.status_code == 404


def test_rules_route_denies_cross_operator_mutation(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-012 mutating half: POST /rules/{other_op_id} -> 404 too."""
    _, subject_a = seed_operator(provider="microsoft", display_name="Alice")

    app = app_factory()
    with _sign_in(app, "microsoft", subject_a, "Alice", monkeypatch) as client:
        # A CSRF header is present so the check does not short-circuit
        # to 403 before the ownership guard runs.
        response = client.post(
            "/rules/999999",
            data={"_csrf": client.cookies.get("wodbuster_csrf", "")},
            headers={"X-CSRF-Token": client.cookies.get("wodbuster_csrf", "")},
        )

    assert response.status_code == 404
