"""Component test: rule mutations sync the booking scheduler (US1.10).

Verifies the hot-reload contract: creating, updating, or deleting a
rule through the HTTP API must update the in-memory APScheduler
job set in the same request. Uses a stopped :class:`BackgroundScheduler`
and a stub executor so we can inspect job state directly without
firing anything.

Kept in its own file so the existing ``test_rules_routes.py`` stays
focused on the CRUD contract; scheduler side-effects are a distinct
concern.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.engine import Engine

from wodbuster_worker.persistence.models import SchedulerRule
from wodbuster_worker.scheduler.rule_jobs import BOOKING_JOB_ID_PREFIX


def _sign_in(
    app: FastAPI,
    subject_id: str,
    display_name: str,
    monkeypatch: pytest.MonkeyPatch,
) -> TestClient:
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


def _valid_form(csrf: str, *, days: tuple[int, ...] = (2,)) -> dict[str, str]:
    form: dict[str, str] = {
        "_csrf": csrf,
        "class_type": "WOD",
        "class_time": "21:30",
        "booking_opens_days_before": "2",
        "booking_opens_at": "21:30",
    }
    for day in days:
        form[f"day_of_week_{day}"] = "on"
    return form


def _seed_scheduler_on_app(app: FastAPI) -> BackgroundScheduler:
    """Attach a stopped booking scheduler + fake executor to ``app.state``.

    The routes' ``_sync_after_*`` helpers look these two attributes up
    on request.app.state; setting them is enough to exercise the
    scheduler side-effect. We never call ``scheduler.start()`` — the
    tests only assert on the in-memory job set.
    """
    scheduler = BackgroundScheduler(timezone="UTC")
    app.state.booking_scheduler = scheduler
    app.state.booking_executor = MagicMock()
    return scheduler


def _booking_jobs(scheduler: BackgroundScheduler) -> list[str]:
    return [
        j.id for j in scheduler.get_jobs() if j.id.startswith(BOOKING_JOB_ID_PREFIX)
    ]


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_create_registers_one_job_per_new_rule(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    scheduler = _seed_scheduler_on_app(app)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        response = client.post(
            "/rules",
            data=_valid_form(csrf, days=(0, 2, 4)),  # Mon+Wed+Fri fan-out
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    jobs = _booking_jobs(scheduler)
    assert len(jobs) == 3  # one per attendance day


def test_update_replaces_the_existing_job_with_fresh_trigger(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    scheduler = _seed_scheduler_on_app(app)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules", data=_valid_form(csrf, days=(2,)), headers=_csrf_headers(client)
        )
        # Grab the rule id straight from the DB.
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=postgres_engine)
        with factory() as session:
            rule_id = session.query(SchedulerRule).filter_by(operator_id=op_id).one().id

        original_trigger = scheduler.get_job(f"{BOOKING_JOB_ID_PREFIX}{rule_id}").trigger  # type: ignore[union-attr]

        response = client.post(
            f"/rules/{rule_id}",
            data={
                "_csrf": csrf,
                "day_of_week": "4",  # Wed -> Fri
                "class_type": "WOD",
                "class_time": "21:30",
                "booking_opens_days_before": "3",  # new
                "booking_opens_at": "22:00",  # new
            },
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    job = scheduler.get_job(f"{BOOKING_JOB_ID_PREFIX}{rule_id}")
    assert job is not None
    # Exactly one job for this rule — the update replaced, not added.
    assert len(_booking_jobs(scheduler)) == 1
    # New trigger reflects the updated schedule.
    assert job.trigger.run_date != original_trigger.run_date


def test_delete_removes_the_job(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    scheduler = _seed_scheduler_on_app(app)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        client.post(
            "/rules", data=_valid_form(csrf, days=(2,)), headers=_csrf_headers(client)
        )
        from sqlalchemy.orm import sessionmaker

        factory = sessionmaker(bind=postgres_engine)
        with factory() as session:
            rule_id = session.query(SchedulerRule).filter_by(operator_id=op_id).one().id

        assert _booking_jobs(scheduler) == [f"{BOOKING_JOB_ID_PREFIX}{rule_id}"]

        response = client.post(
            f"/rules/{rule_id}/delete",
            data={"_csrf": csrf},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    assert _booking_jobs(scheduler) == []


def test_mutations_noop_when_booking_scheduler_not_wired(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The rules CRUD flow must still work when the booking stack is
    missing (no ``wodbuster_gym`` / ``wodbuster_idu`` configured)."""
    _op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()
    # Deliberately do NOT seed booking_scheduler / booking_executor.

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        csrf = client.cookies["wodbuster_csrf"]
        response = client.post(
            "/rules", data=_valid_form(csrf, days=(2,)), headers=_csrf_headers(client)
        )

    # CRUD succeeded even without the scheduler.
    assert response.status_code == 303
