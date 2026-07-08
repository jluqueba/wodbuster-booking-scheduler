"""Component tests for the scheduler-rule CRUD routes (US5.T1, US5.T2 partial).

Exercises the five routes end-to-end against real Postgres. The
:class:`~wodbuster_worker.scheduler.scheduler.BackgroundScheduler`
never starts because ``TestClient`` calls ``app_factory`` which
returns a fresh app without wiring the lifespan for us; hot-reload
tests would need a real scheduler and are deferred until US1.10 lands
the booking jobs that actually need reloading.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from wodbuster_worker.persistence.models import ClassPreference, SchedulerRule


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


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("wodbuster_csrf")
    assert token, "expected wodbuster_csrf cookie after sign-in"
    return {"X-CSRF-Token": token}


def _valid_rule_form(csrf: str) -> dict[str, str]:
    return {
        "_csrf": csrf,
        "day_of_week": "2",
        "window_offset_hours": "48",
        "preference_0_class_type": "WOD",
        "preference_0_time_slot": "21:30",
    }


def test_rules_list_unauthenticated_redirects_to_login(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/rules")
    assert response.status_code == 302
    assert "/auth/" in response.headers["location"]


def test_rules_list_empty_state_renders(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/rules")

    assert response.status_code == 200
    assert "No rules yet" in response.text


def test_create_rule_persists_and_redirects_to_list(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        response = client.post(
            "/rules",
            data=_valid_rule_form(csrf),
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/rules"

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        rules = session.query(SchedulerRule).filter_by(operator_id=op_id).all()
        assert len(rules) == 1
        assert rules[0].day_of_week == 2
        assert rules[0].window_offset_hours == 48
        prefs = (
            session.query(ClassPreference)
            .filter_by(rule_id=rules[0].id)
            .order_by(ClassPreference.order_index)
            .all()
        )
        assert [p.class_type for p in prefs] == ["WOD"]
        assert [p.target_time_slot for p in prefs] == ["21:30"]


def test_create_rule_with_invalid_form_re_renders_with_errors(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        # Missing preferences → validation failure.
        response = client.post(
            "/rules",
            data={
                "_csrf": csrf,
                "day_of_week": "2",
                "window_offset_hours": "48",
            },
            headers=_csrf_headers(client),
        )

    assert response.status_code == 422
    assert "At least one preference is required" in response.text
    # No row created.
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        assert session.query(SchedulerRule).filter_by(operator_id=op_id).count() == 0


def test_create_rule_without_csrf_is_forbidden(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        # Neither the header nor the ``_csrf`` form field is present.
        # The double-submit check has to reject on either path.
        response = client.post(
            "/rules",
            data={
                "day_of_week": "2",
                "window_offset_hours": "48",
                "preference_0_class_type": "WOD",
                "preference_0_time_slot": "21:30",
            },
        )

    assert response.status_code == 403


def test_list_shows_own_rules_and_no_others(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Alice creates a rule; Bob signs in and must not see it.
    _, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    _, subject_b = seed_operator(provider="microsoft", display_name="Bob")
    app = app_factory()

    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_rule_form(csrf),
            headers=_csrf_headers(client),
        )

    with _sign_in(app, subject_b, "Bob", monkeypatch) as client:
        response = client.get("/rules")

    assert response.status_code == 200
    assert "No rules yet" in response.text
    assert "WOD" not in response.text


def test_edit_rule_updates_preferences_in_place(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_rule_form(csrf),
            headers=_csrf_headers(client),
        )
        # Fetch id.
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=postgres_engine)
        with factory() as session:
            rule_id = (
                session.query(SchedulerRule).filter_by(operator_id=op_id).one().id
            )

        # Edit: add a fallback, bump offset.
        response = client.post(
            f"/rules/{rule_id}",
            data={
                "_csrf": csrf,
                "day_of_week": "2",
                "window_offset_hours": "72",
                "preference_0_class_type": "WOD",
                "preference_0_time_slot": "21:30",
                "preference_1_class_type": "Halterofilia",
                "preference_1_time_slot": "22:30",
            },
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    with factory() as session:
        rule = session.query(SchedulerRule).filter_by(id=rule_id).one()
        assert rule.window_offset_hours == 72
        prefs = (
            session.query(ClassPreference)
            .filter_by(rule_id=rule_id)
            .order_by(ClassPreference.order_index)
            .all()
        )
        assert [p.class_type for p in prefs] == ["WOD", "Halterofilia"]
        assert [p.order_index for p in prefs] == [0, 1]


def test_edit_rule_of_other_operator_returns_404(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_a_id, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    _, subject_b = seed_operator(provider="microsoft", display_name="Bob")
    app = app_factory()

    # Alice creates a rule.
    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_rule_form(csrf),
            headers=_csrf_headers(client),
        )
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        alice_rule_id = (
            session.query(SchedulerRule).filter_by(operator_id=op_a_id).one().id
        )

    # Bob tries to fetch AND mutate.
    with _sign_in(app, subject_b, "Bob", monkeypatch) as client:
        r_read = client.get(f"/rules/{alice_rule_id}")
        assert r_read.status_code == 404
        r_write = client.post(
            f"/rules/{alice_rule_id}",
            data=_valid_rule_form(client.cookies["wodbuster_csrf"]),
            headers=_csrf_headers(client),
        )
        assert r_write.status_code == 404
        r_delete = client.post(
            f"/rules/{alice_rule_id}/delete",
            data={"_csrf": client.cookies["wodbuster_csrf"]},
            headers=_csrf_headers(client),
        )
        assert r_delete.status_code == 404

    # Alice's row still exists untouched.
    with factory() as session:
        assert session.query(SchedulerRule).filter_by(id=alice_rule_id).count() == 1


def test_delete_rule_removes_row_and_preferences(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_rule_form(csrf),
            headers=_csrf_headers(client),
        )
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=postgres_engine)
        with factory() as session:
            rule_id = (
                session.query(SchedulerRule).filter_by(operator_id=op_id).one().id
            )

        response = client.post(
            f"/rules/{rule_id}/delete",
            data={"_csrf": csrf},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    with factory() as session:
        assert session.query(SchedulerRule).filter_by(id=rule_id).count() == 0
        assert (
            session.query(ClassPreference).filter_by(rule_id=rule_id).count() == 0
        )


def test_edit_form_prefills_current_values(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_rule_form(csrf),
            headers=_csrf_headers(client),
        )
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=postgres_engine)
        with factory() as session:
            rule = session.query(SchedulerRule).first()
            assert rule is not None
            rule_id = rule.id

        response = client.get(f"/rules/{rule_id}")

    assert response.status_code == 200
    # Day of week option is selected (accept any whitespace between
    # ``value="2"`` and the ``selected`` attribute).
    import re

    assert re.search(r'value="2"\s+selected', response.text), (
        "expected the Wednesday option to be pre-selected"
    )
    # Preference values echoed.
    assert 'value="WOD"' in response.text
    assert 'value="21:30"' in response.text
