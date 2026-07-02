"""US9.T4 — CSRF protection unit tests.

Covers:

- ``verify_csrf`` raises 403 when the session has no CSRF token.
- ``verify_csrf`` raises 403 when the request presents a token that
  does not match the session token.
- ``verify_csrf`` accepts a valid ``X-CSRF-Token`` header (HTMX path).
- ``verify_csrf`` accepts a valid ``_csrf`` form field (plain-HTML
  fallback).
- Integration: ``POST /auth/logout`` returns 403 without a token and
  302 with a valid token.

Uses :class:`fastapi.testclient.TestClient` at the app layer. No
Postgres dependency; the CSRF module reads only from the session and
request.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager

import pytest
from fastapi import Depends, FastAPI, Request
from fastapi.testclient import TestClient
from starlette.middleware import Middleware
from starlette.middleware.sessions import SessionMiddleware

from wodbuster_worker.auth.csrf import (
    CSRF_COOKIE_NAME,
    issue_csrf_token,
    verify_csrf,
)


def _build_csrf_test_app() -> FastAPI:
    """Minimal app: session middleware + one protected POST route.

    Deliberately does not go through :func:`create_app` because that
    pulls in the whole auth wiring; here we only want to exercise the
    CSRF dependency in isolation.
    """

    @asynccontextmanager
    async def _lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield

    app = FastAPI(
        lifespan=_lifespan,
        middleware=[
            Middleware(
                SessionMiddleware,
                secret_key="a" * 32,
                session_cookie="wodbuster_session",
                same_site="lax",
                https_only=False,
            )
        ],
    )

    @app.get("/prime")
    def prime(request: Request) -> dict[str, str]:
        token = issue_csrf_token(request)
        return {"csrf_token": token}

    @app.post("/do", dependencies=[Depends(verify_csrf)])
    def do_mutating() -> dict[str, str]:
        return {"status": "ok"}

    return app


@pytest.fixture
def csrf_client() -> Callable[[], TestClient]:
    def _make() -> TestClient:
        return TestClient(_build_csrf_test_app())

    return _make


def test_post_without_token_is_rejected(
    csrf_client: Callable[[], TestClient],
) -> None:
    with csrf_client() as client:
        # No session at all — no session cookie, no header.
        response = client.post("/do")
    assert response.status_code == 403
    assert "CSRF" in response.json()["detail"]


def test_post_with_wrong_token_is_rejected(
    csrf_client: Callable[[], TestClient],
) -> None:
    with csrf_client() as client:
        prime = client.get("/prime")
        assert prime.status_code == 200
        response = client.post("/do", headers={"X-CSRF-Token": "not-the-real-token"})
    assert response.status_code == 403


def test_post_with_correct_header_is_accepted(
    csrf_client: Callable[[], TestClient],
) -> None:
    with csrf_client() as client:
        prime = client.get("/prime")
        token = prime.json()["csrf_token"]
        response = client.post("/do", headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_post_with_correct_form_field_is_accepted(
    csrf_client: Callable[[], TestClient],
) -> None:
    with csrf_client() as client:
        prime = client.get("/prime")
        token = prime.json()["csrf_token"]
        response = client.post("/do", data={"_csrf": token})
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Integration: /auth/logout route uses verify_csrf via Depends.
# ---------------------------------------------------------------------------
#
# Exercises the actual wiring on the auth router. Requires only the
# session middleware (no DB), because logout does not touch Postgres.


def test_logout_without_csrf_token_is_rejected(
    app_factory_no_db: Callable[..., FastAPI],
) -> None:
    app = app_factory_no_db()
    with TestClient(app) as client:
        response = client.post("/auth/logout")
    assert response.status_code == 403


def test_logout_with_valid_csrf_token_redirects(
    app_factory_no_db: Callable[..., FastAPI],
) -> None:
    app = app_factory_no_db()
    with TestClient(app, follow_redirects=False) as client:
        # Prime a session with a CSRF token by hitting a helper route.
        prime = client.get("/_test/prime")
        assert prime.status_code == 200
        token = prime.json()["csrf_token"]

        response = client.post("/auth/logout", headers={"X-CSRF-Token": token})
    assert response.status_code == 302
    assert response.headers["location"] == "/auth/microsoft/login"
    # The CSRF cookie is unset on logout.
    set_cookie = response.headers.get_list("set-cookie")
    assert any(CSRF_COOKIE_NAME in c and "Max-Age=0" in c for c in set_cookie)


@pytest.fixture
def app_factory_no_db() -> Callable[..., FastAPI]:
    """Factory building the real app without touching Postgres.

    Used by the logout-integration tests above. Adds a helper
    ``/_test/prime`` route that seeds the session CSRF token so the
    logout POST can present a matching value.
    """
    from wodbuster_worker.app import create_app
    from wodbuster_worker.config import Settings
    from wodbuster_worker.security.keyvault import Secrets

    def _build() -> FastAPI:
        settings = Settings(
            wodbuster_env="local",
            postgres_host="localhost",
            postgres_db="wodbuster",
            postgres_user="wodbuster",
            postgres_password="wodbuster",
        )
        secrets = Secrets(session_encryption_secret="a" * 32)
        app = create_app(settings=settings, secrets=secrets)

        @app.get("/_test/prime")
        def _prime(request: Request) -> dict[str, str]:
            return {"csrf_token": issue_csrf_token(request)}

        return app

    return _build
