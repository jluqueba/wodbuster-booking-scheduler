"""US9.T1 — anonymous requests to protected routes redirect cleanly.

Verifies CC-011:

- Every protected route returns 302 to ``/auth/microsoft/login`` when
  the request carries no session.
- The response body is empty; no operator data (display name, rule
  names, booking history) leaks through the redirect.

The root path ``/`` is intentionally NOT protected: it renders a
landing hero for unauthenticated visitors and the operator dashboard
once the session is seated. Protected routes list ``/cookie`` and
``/rules`` (added when their user stories landed).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

# Routes that MUST 302 to the login flow when unauthenticated.
PROTECTED_ROUTES = ["/cookie", "/rules"]


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


def test_anonymous_root_renders_landing_page(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
) -> None:
    """The root path shows a marketing / sign-in landing when anonymous."""
    _, _ = seed_operator(display_name="Alice Wonderland-42")
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/")

    assert response.status_code == 200
    body = response.text
    # Microsoft and Google are surfaced on the landing; GitHub is not.
    assert "Sign in with Microsoft" in body
    assert "Sign in with Google" in body
    assert "Sign in with GitHub" not in body
    assert 'href="/auth/microsoft/login"' in body
    assert 'href="/auth/google/login"' in body
    assert 'href="/auth/github/login"' not in body
    # No operator data leaks through the anonymous view.
    assert "Alice Wonderland-42" not in body


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
