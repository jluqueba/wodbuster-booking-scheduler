"""Component tests for the scheduler-rule CRUD routes (rule model v2).

Exercises the multi-day fan-out create flow, single-day edit, delete,
cross-operator isolation, second-shot pairing, and the ``/api/classes``
picker endpoint.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

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


def _csrf_headers(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("wodbuster_csrf")
    assert token, "expected wodbuster_csrf cookie after sign-in"
    return {"X-CSRF-Token": token}


def _valid_create_form(
    csrf: str,
    *,
    days: tuple[int, ...] = (2,),
    second_shot: bool = False,
) -> dict[str, str]:
    """Build a valid multi-day create form. Defaults to Wednesday only."""
    form: dict[str, str] = {
        "_csrf": csrf,
        "class_type": "WOD",
        "class_time": "21:30",
        "booking_opens_days_before": "2",
        "booking_opens_at": "21:30",
    }
    for day in days:
        form[f"day_of_week_{day}"] = "on"
    if second_shot:
        form["second_shot_class_type"] = "Halterofilia"
        form["second_shot_class_time"] = "20:30"
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

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        rules = session.query(SchedulerRule).filter_by(operator_id=op_id).all()
        assert len(rules) == 1
        rule = rules[0]
        assert rule.day_of_week == 2
        assert rule.class_type == "WOD"
        assert rule.class_time == "21:30"
        assert rule.booking_opens_days_before == 2
        assert rule.booking_opens_at == "21:30"
        assert rule.second_shot_class_type is None
        assert rule.second_shot_class_time is None


def test_create_multi_day_fans_out_to_n_rows(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mon + Wed + Fri produces exactly three rules sharing every other field."""
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

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        rules = (
            session.query(SchedulerRule)
            .filter_by(operator_id=op_id)
            .order_by(SchedulerRule.day_of_week)
            .all()
        )
        assert [r.day_of_week for r in rules] == [0, 2, 4]
        for rule in rules:
            assert rule.class_type == "WOD"
            assert rule.class_time == "21:30"
            assert rule.booking_opens_days_before == 2
            assert rule.booking_opens_at == "21:30"


def test_create_with_second_shot_persists_both_fields(
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
            data=_valid_create_form(csrf, days=(2,), second_shot=True),
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303

    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        rule = session.query(SchedulerRule).filter_by(operator_id=op_id).one()
        assert rule.second_shot_class_type == "Halterofilia"
        assert rule.second_shot_class_time == "20:30"


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
                "class_type": "WOD",
                "class_time": "21:30",
                "booking_opens_days_before": "2",
                "booking_opens_at": "21:30",
            },
            headers=_csrf_headers(client),
        )

    assert response.status_code == 422
    assert "Select at least one day" in response.text

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
        response = client.post(
            "/rules",
            data={
                "day_of_week_2": "on",
                "class_type": "WOD",
                "class_time": "21:30",
                "booking_opens_days_before": "2",
                "booking_opens_at": "21:30",
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


def test_edit_updates_all_fields_in_place(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    factory = sessionmaker(bind=postgres_engine)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_create_form(csrf, days=(2,)),
            headers=_csrf_headers(client),
        )
        with factory() as session:
            rule_id = session.query(SchedulerRule).filter_by(operator_id=op_id).one().id

        response = client.post(
            f"/rules/{rule_id}",
            data={
                "_csrf": csrf,
                "day_of_week": "4",  # Wed -> Fri
                "class_type": "Cross Training",
                "class_time": "07:30",
                "booking_opens_days_before": "3",
                "booking_opens_at": "22:00",
                "second_shot_class_type": "WOD",
                "second_shot_class_time": "08:30",
            },
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    with factory() as session:
        rule = session.query(SchedulerRule).filter_by(id=rule_id).one()
        assert rule.day_of_week == 4
        assert rule.class_type == "Cross Training"
        assert rule.class_time == "07:30"
        assert rule.booking_opens_days_before == 3
        assert rule.booking_opens_at == "22:00"
        assert rule.second_shot_class_type == "WOD"
        assert rule.second_shot_class_time == "08:30"


def test_edit_form_prefills_current_values(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import re

    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    factory = sessionmaker(bind=postgres_engine)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_create_form(csrf, days=(2,)),
            headers=_csrf_headers(client),
        )
        with factory() as session:
            rule_id = session.query(SchedulerRule).one().id

        response = client.get(f"/rules/{rule_id}")

    assert response.status_code == 200
    # Wednesday should be pre-selected in the day dropdown.
    assert re.search(r'value="2"\s+selected', response.text)
    # Time and class type echoed back.
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


def test_delete_rule_removes_row(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    factory = sessionmaker(bind=postgres_engine)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules",
            data=_valid_create_form(csrf, days=(2,)),
            headers=_csrf_headers(client),
        )
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

    The form still renders, but the class-type / time dropdowns are
    disabled and the submit button is greyed out until the operator
    seeds a cookie. Free-text entry is intentionally NOT offered — a
    typo would silently create a rule that never books.
    """
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    assert app.state.wodbuster_client is None

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/rules/api/classes")

    assert response.status_code == 200
    body = response.json()
    assert body == {"class_types": [], "time_slots": [], "available": False}


def test_new_form_disables_selects_when_picker_unavailable(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the picker fails, the class-type combo and submit are disabled."""
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/rules/new")

    assert response.status_code == 200
    # The picker note explains the empty state.
    assert "Live class list unavailable" in response.text
    # Class-type stays a <select> (must match schedule string exactly).
    assert "<select" in response.text
    # Time fields render as the custom wb-time-picker widget (no
    # AM/PM ambiguity, always 24h; wire format remains HH:MM via the
    # hidden input).
    assert 'class="wb-time-picker"' in response.text
    assert response.text.count('data-time-picker="class_time"') == 1
    assert response.text.count('data-time-picker="booking_opens_at"') == 1
    assert response.text.count('data-time-picker="second_shot_class_time"') == 1
    # The init script is present so the widgets sync into the
    # hidden inputs on user edit.
    assert "wb-time-picker" in response.text
    assert "initTimePicker" in response.text
    # Both class-type combos and the submit button carry the disabled
    # marker when the picker is empty.
    assert response.text.count("disabled") >= 3


def test_new_form_time_pickers_render_hour_and_minute_selects(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The 24h picker exposes hour options 00-23 and minute options 00-55."""
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/rules/new")

    assert response.status_code == 200
    # Sample of hour options — should span the full 24h range.
    assert '<option value="00">00</option>' in response.text
    assert '<option value="12">12</option>' in response.text
    assert '<option value="23">23</option>' in response.text
    # Sample of minute options at 5-minute granularity.
    assert '<option value="00">00</option>' in response.text
    assert '<option value="30">30</option>' in response.text
    assert '<option value="55">55</option>' in response.text
    # And crucially: no native <input type="time"> that would surface
    # AM/PM on 12h locales.
    assert 'type="time"' not in response.text
