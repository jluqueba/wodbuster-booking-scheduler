"""Component tests for the scheduler-rule CRUD routes (US-005 form uplift).

Exercises the multi-day fan-out create flow, single-day edit, delete,
cross-operator isolation, and the ``/api/classes`` picker endpoint.
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


def _valid_create_form(csrf: str, *, days: tuple[int, ...] = (2,)) -> dict[str, str]:
    """Build a valid multi-day create form. Defaults to Wednesday only."""
    form: dict[str, str] = {
        "_csrf": csrf,
        "time_slot": "21:30",
        "preference_0_class_type": "WOD",
    }
    for day in days:
        form[f"day_of_week_{day}"] = "on"
    return form


# --- List --------------------------------------------------------------


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


# --- Create (multi-day fan-out) ----------------------------------------


def test_create_single_day_persists_one_row(
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
            data=_valid_create_form(csrf, days=(2,)),
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
        prefs = session.query(ClassPreference).filter_by(rule_id=rules[0].id).all()
        assert [p.class_type for p in prefs] == ["WOD"]
        # Time gets denormalised into every preference row.
        assert [p.target_time_slot for p in prefs] == ["21:30"]


def test_create_multi_day_fans_out_to_n_rows(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mon + Wed + Fri produces exactly three rules sharing time + prefs."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        response = client.post(
            "/rules",
            data=_valid_create_form(csrf, days=(0, 2, 4)),
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        rules = (
            session.query(SchedulerRule)
            .filter_by(operator_id=op_id)
            .order_by(SchedulerRule.day_of_week)
            .all()
        )
        assert [r.day_of_week for r in rules] == [0, 2, 4]
        # Every row has the same time + preference chain.
        for rule in rules:
            prefs = (
                session.query(ClassPreference)
                .filter_by(rule_id=rule.id)
                .order_by(ClassPreference.order_index)
                .all()
            )
            assert [p.class_type for p in prefs] == ["WOD"]
            assert [p.target_time_slot for p in prefs] == ["21:30"]


def test_create_uses_global_booking_lead_offset(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every new rule receives ``settings.wodbuster_booking_lead_hours``."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory(wodbuster_booking_lead_hours=72)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_create_form(csrf),
            headers=_csrf_headers(client),
        )

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        rule = session.query(SchedulerRule).filter_by(operator_id=op_id).one()
        assert rule.window_offset_hours == 72


def test_create_with_no_days_re_renders_with_error(
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
            data={
                "_csrf": csrf,
                "time_slot": "21:30",
                "preference_0_class_type": "WOD",
            },
            headers=_csrf_headers(client),
        )

    assert response.status_code == 422
    assert "Select at least one day" in response.text

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        assert session.query(SchedulerRule).filter_by(operator_id=op_id).count() == 0


def test_create_without_csrf_is_forbidden(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        # No _csrf field and no header — must 403.
        response = client.post(
            "/rules",
            data={
                "day_of_week_2": "on",
                "time_slot": "21:30",
                "preference_0_class_type": "WOD",
            },
        )
    assert response.status_code == 403


# --- List rendering with real rows -------------------------------------


def test_list_shows_own_rules_and_no_others(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    _, subject_b = seed_operator(provider="microsoft", display_name="Bob")
    app = app_factory()

    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_create_form(csrf, days=(2,)),
            headers=_csrf_headers(client),
        )

    with _sign_in(app, subject_b, "Bob", monkeypatch) as client:
        response = client.get("/rules")
    assert response.status_code == 200
    assert "No rules yet" in response.text
    assert "WOD" not in response.text


# --- Edit (single day) -------------------------------------------------


def test_edit_updates_day_time_and_preferences_in_place(
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
            data=_valid_create_form(csrf, days=(2,)),
            headers=_csrf_headers(client),
        )
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=postgres_engine)
        with factory() as session:
            rule_id = session.query(SchedulerRule).filter_by(operator_id=op_id).one().id

        response = client.post(
            f"/rules/{rule_id}",
            data={
                "_csrf": csrf,
                "day_of_week": "4",  # Wed -> Fri
                "time_slot": "07:30",
                "preference_0_class_type": "Cross Training",
                "preference_1_class_type": "WOD",
            },
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    with factory() as session:
        rule = session.query(SchedulerRule).filter_by(id=rule_id).one()
        assert rule.day_of_week == 4
        prefs = (
            session.query(ClassPreference)
            .filter_by(rule_id=rule_id)
            .order_by(ClassPreference.order_index)
            .all()
        )
        assert [p.class_type for p in prefs] == ["Cross Training", "WOD"]
        # New time replicated across every preference row.
        assert {p.target_time_slot for p in prefs} == {"07:30"}


def test_edit_form_prefills_current_values(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_create_form(csrf, days=(2,)),
            headers=_csrf_headers(client),
        )
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=postgres_engine)
        with factory() as session:
            rule_id = session.query(SchedulerRule).one().id

        response = client.get(f"/rules/{rule_id}")

    assert response.status_code == 200
    # Wednesday should be pre-selected in the day dropdown.
    assert re.search(r'value="2"\s+selected', response.text)
    # Time and preference values echoed back.
    assert "21:30" in response.text
    assert "WOD" in response.text


def test_edit_rule_of_other_operator_returns_404(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_a_id, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    _, subject_b = seed_operator(provider="microsoft", display_name="Bob")
    app = app_factory()

    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_create_form(csrf, days=(2,)),
            headers=_csrf_headers(client),
        )

    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        alice_rule_id = (
            session.query(SchedulerRule).filter_by(operator_id=op_a_id).one().id
        )

    with _sign_in(app, subject_b, "Bob", monkeypatch) as client:
        assert client.get(f"/rules/{alice_rule_id}").status_code == 404
        assert (
            client.post(
                f"/rules/{alice_rule_id}",
                data={"_csrf": client.cookies["wodbuster_csrf"]},
                headers=_csrf_headers(client),
            ).status_code
            == 404
        )
        assert (
            client.post(
                f"/rules/{alice_rule_id}/delete",
                data={"_csrf": client.cookies["wodbuster_csrf"]},
                headers=_csrf_headers(client),
            ).status_code
            == 404
        )

    with factory() as session:
        assert session.query(SchedulerRule).filter_by(id=alice_rule_id).count() == 1


# --- Delete ------------------------------------------------------------


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
            data=_valid_create_form(csrf, days=(2,)),
            headers=_csrf_headers(client),
        )
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=postgres_engine)
        with factory() as session:
            rule_id = session.query(SchedulerRule).filter_by(operator_id=op_id).one().id

        response = client.post(
            f"/rules/{rule_id}/delete",
            data={"_csrf": csrf},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    with factory() as session:
        assert session.query(SchedulerRule).filter_by(id=rule_id).count() == 0
        assert session.query(ClassPreference).filter_by(rule_id=rule_id).count() == 0


# --- /api/classes -------------------------------------------------------


def test_api_classes_unauth_redirects(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/rules/api/classes")
    assert response.status_code == 302
    assert "/auth/" in response.headers["location"]


def test_api_classes_returns_unavailable_when_stack_not_wired(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing cookie stack (no gym / idu) collapses to ``available=false``.

    The form still renders; it just falls back to free-text inputs.
    """
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    # ``app_factory`` builds the app without ``wodbuster_gym`` / ``wodbuster_idu``,
    # so the wodbuster_client is None. Confirm the endpoint degrades gracefully.
    assert app.state.wodbuster_client is None

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/rules/api/classes")

    assert response.status_code == 200
    body = response.json()
    assert body == {"class_types": [], "time_slots": [], "available": False}


def test_new_form_renders_free_text_fallback_when_picker_unavailable(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The empty picker path renders the free-text hint copy."""
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/rules/new")

    assert response.status_code == 200
    # Picker note is present because no live class list was fetched.
    assert "Live class list unavailable" in response.text
