"""Component tests for the public email unsubscribe route (ADR-0011)."""

from __future__ import annotations

from collections.abc import Callable

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine

from wodbuster_worker.notifications.unsubscribe import make_unsubscribe_token


def test_unsubscribe_disables_operational_preferences(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
) -> None:
    op_id, _ = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    with TestClient(app) as client:
        secret = app.state.email_unsubscribe_secret
        token = make_unsubscribe_token(op_id, secret=secret)
        resp = client.get(f"/unsubscribe?t={token}")
    assert resp.status_code == 200
    with postgres_engine.connect() as conn:
        prefs = conn.execute(
            text("SELECT email_preferences FROM operator_profile WHERE id = :id"),
            {"id": op_id},
        ).scalar_one()
    # Operational categories off; the transactional 'account' concept is not a key.
    assert prefs == {"bookings": False, "session_alerts": False}


def test_unsubscribe_invalid_token_is_rejected(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app) as client:
        resp = client.get("/unsubscribe?t=not-a-real-token")
    assert resp.status_code == 400
