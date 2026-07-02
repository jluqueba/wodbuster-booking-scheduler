"""US9.T1 — anonymous requests to protected routes redirect cleanly.

Verifies CC-011:

- Every protected route returns 302 to ``/auth/microsoft/login`` when
  the request carries no session.
- The response body is empty; no operator data (display name, rule
  names, booking history) leaks through the redirect.

The dashboard route (``/``) is the only protected route in scope for
US-009 core. As additional routes land, they are added to the
``PROTECTED_ROUTES`` list below.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Extended as new protected routes land (rules, cookie-paste, history).
# For US-009 core only ``/`` exists. Left as a list so future stories
# can grow it without changing the test structure.
PROTECTED_ROUTES = ["/"]


@pytest.mark.parametrize("path", PROTECTED_ROUTES)
def test_anonymous_request_redirects_to_login(
    path: str,
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
) -> None:
    # Seed an operator with a distinctive display name so the assertion
    # below can prove that the redirect body does not leak it.
    _, _ = seed_operator(display_name="Alice Wonderland-42")

    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        response = client.get(path)

    assert response.status_code == 302
    assert response.headers["location"] == "/auth/microsoft/login"

    # The body must be empty (or at most a minimal, static redirect
    # marker with no operator data). Assert no seeded operator string
    # appears.
    body = response.text
    assert "Alice Wonderland-42" not in body
    assert "operator" not in body.lower()


def test_health_route_is_public(app_factory: Callable[..., FastAPI]) -> None:
    """``/health`` stays open. It is the Container App liveness probe."""
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_auth_login_route_is_public(
    app_factory: Callable[..., FastAPI],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/auth/{provider}/login`` must not itself be gated."""
    app = app_factory()

    # Stub Authlib so the test does not try to reach the real IdP.
    async def fake_redirect(*_args: Any, **_kwargs: Any) -> Any:
        from fastapi.responses import RedirectResponse

        return RedirectResponse(url="https://idp.example/authorize", status_code=302)

    ms_client = app.state.oauth.create_client("microsoft")
    monkeypatch.setattr(ms_client, "authorize_redirect", fake_redirect)

    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/auth/microsoft/login")

    assert response.status_code == 302
    assert response.headers["location"].startswith("https://idp.example/")
