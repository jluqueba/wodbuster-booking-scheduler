"""Component tests for the global gym switcher (multi-gym UX, approach A).

Covers: the sole gym auto-selects and shows a static label (no switcher);
owning >1 gym renders the switcher plus a "choose a gym" prompt; selecting
a gym scopes the gym-scoped pages to it and persists across requests; and
selecting another user's gym is a 404 with no state change.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.persistence.models import SchedulerRule


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


def _add_gym(engine: Engine, operator_id: int, *, slug: str, display_name: str) -> int:
    """Insert an extra active gym account and return its id."""
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO gym_account (user_id, gym_slug, display_name, idu) "
                    "VALUES (:op, :s, :n, :idu) RETURNING id"
                ),
                {"op": operator_id, "s": slug, "n": display_name, "idu": "0" * 32},
            ).scalar_one()
        )


def _sole_gym_id(engine: Engine, operator_id: int) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("SELECT id FROM gym_account WHERE user_id = :op ORDER BY id LIMIT 1"),
                {"op": operator_id},
            ).scalar_one()
        )


def _seed_rule(session: Session, gym_account_id: int, *, class_type: str) -> None:
    session.add(
        SchedulerRule(
            gym_account_id=gym_account_id,
            day_of_week=2,
            class_type=class_type,
            class_time="21:30",
            booking_opens_days_before=2,
            booking_opens_at="21:30",
            active=True,
        )
    )
    session.flush()


def test_single_gym_shows_label_without_switcher(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(display_name="Solo Op")
    app = app_factory()
    with _sign_in(app, subject, "Solo Op", monkeypatch) as client:
        response = client.get("/rules")
    assert response.status_code == 200
    # The sole gym renders as a static label, not a selectable switcher.
    assert "wb-nav__gym-current" in response.text
    assert 'name="gym_account_id"' not in response.text
    assert "/gyms/select" not in response.text


def test_two_gyms_render_switcher_and_prompt(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(display_name="Multi Op")
    _add_gym(postgres_engine, op_id, slug="secondgym", display_name="Second Gym")
    app = app_factory()
    with _sign_in(app, subject, "Multi Op", monkeypatch) as client:
        response = client.get("/rules")
    assert response.status_code == 200
    assert 'name="gym_account_id"' in response.text
    assert "/gyms/select" in response.text
    assert "Second Gym" in response.text
    # No selection yet -> the choose-a-gym prompt is shown.
    assert "Choose a gym from the selector above" in response.text


def test_select_gym_scopes_pages_and_clears_prompt(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(display_name="Multi Op")
    gym2 = _add_gym(postgres_engine, op_id, slug="secondgym", display_name="Second Gym")
    gym1 = _sole_gym_id(postgres_engine, op_id)
    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        _seed_rule(session, gym1, class_type="GymOneWOD")
        _seed_rule(session, gym2, class_type="GymTwoWOD")
        session.commit()

    app = app_factory()
    with _sign_in(app, subject, "Multi Op", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        selected = client.post(
            "/gyms/select",
            data={"_csrf": csrf, "gym_account_id": str(gym2), "next": "/rules"},
            headers={"X-CSRF-Token": csrf},
        )
        assert selected.status_code == 303
        assert selected.headers["location"] == "/rules"
        response = client.get("/rules")

    assert response.status_code == 200
    assert "GymTwoWOD" in response.text
    assert "GymOneWOD" not in response.text
    assert "Choose a gym from the selector above" not in response.text


def test_selection_persists_across_pages(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(display_name="Multi Op")
    gym2 = _add_gym(postgres_engine, op_id, slug="secondgym", display_name="Second Gym")
    app = app_factory()
    with _sign_in(app, subject, "Multi Op", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/gyms/select",
            data={"_csrf": csrf, "gym_account_id": str(gym2), "next": "/rules"},
            headers={"X-CSRF-Token": csrf},
        )
        response = client.get("/history")
    assert response.status_code == 200
    assert f'value="{gym2}" selected' in response.text


def test_select_other_users_gym_is_404(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject_a = seed_operator(display_name="Alice")
    op_b, _ = seed_operator(display_name="Bob")
    gym_b = _sole_gym_id(postgres_engine, op_b)
    app = app_factory()
    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        response = client.post(
            "/gyms/select",
            data={"_csrf": csrf, "gym_account_id": str(gym_b), "next": "/rules"},
            headers={"X-CSRF-Token": csrf},
        )
    assert response.status_code == 404
