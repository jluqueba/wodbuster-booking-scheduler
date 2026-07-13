"""Smoke tests for the UI polish pass.

Covers the surfaces added or reworked in the polish PR:

- Favicon served from ``/static/favicon.svg``.
- FAQ page renders behind auth with the section titles present.
- Nav renders the "WodBuster Booking Scheduler" full label and the
  new FAQ tab.
- Rules list active status uses the new green ``wb-chip--success``
  class rather than the yellow accent chip.
- Dashboard countdown block is present when the operator has an
  active rule and links to the next window ISO datetime.
- Confirm-modal partial is included on every authed page.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine


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


def _seed_active_rule(engine: Engine, *, operator_id: int) -> int:
    """Insert a Wed-attendance rule opening 2d before at 21:30."""
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule "
                    "(operator_id, day_of_week, class_type, class_time, "
                    "booking_opens_days_before, booking_opens_at, active) "
                    "VALUES (:op, 2, 'WOD', '21:30', 2, '21:30', true) "
                    "RETURNING id"
                ),
                {"op": operator_id},
            ).scalar_one()
        )


def test_favicon_served_as_svg(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/static/favicon.svg")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg")
    assert response.text.startswith("<?xml")


def test_faq_page_renders_behind_auth(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/faq")

    assert response.status_code == 200
    # Section titles must be present.
    assert "Getting started" in response.text
    assert "Cookie" in response.text
    assert "Rules" in response.text
    assert "Troubleshooting" in response.text
    # Question about the picker being empty is in the Rules section.
    assert "class-type dropdown is empty" in response.text


def test_faq_route_gated_by_auth(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/faq")
    assert response.status_code == 302
    assert "/auth/" in response.headers["location"]


def test_nav_renders_full_brand_and_faq_tab(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "WodBuster Booking Scheduler" in response.text
    assert "wb-nav__brand-full" in response.text
    assert "wb-nav__brand-short" in response.text
    assert 'href="/faq"' in response.text


def test_rules_list_active_status_uses_success_chip(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    _seed_active_rule(postgres_engine, operator_id=op_id)
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/rules")

    assert response.status_code == 200
    assert 'class="wb-chip wb-chip--success">active</span>' in response.text
    # And the old accent chip is not used for the status anymore.
    assert 'wb-chip--accent">active' not in response.text


def test_dashboard_countdown_present_when_rule_active(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    _seed_active_rule(postgres_engine, operator_id=op_id)
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert 'class="wb-countdown"' in response.text
    assert "data-next-window=" in response.text
    # The rendered ISO datetime should be in the future.
    import re

    m = re.search(r'data-next-window="([^"]+)"', response.text)
    assert m is not None
    parsed = datetime.fromisoformat(m.group(1))
    assert parsed > datetime.now(tz=UTC) - timedelta(seconds=5)


def test_dashboard_countdown_empty_when_no_rules(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "wb-countdown--empty" in response.text
    assert "Add a rule" in response.text


def test_confirm_modal_partial_included_on_authed_pages(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        # `/cookie` needs the cookie stack wired (not present in this
        # test app), so it 503s and is excluded from the sweep.
        for path in ("/", "/rules", "/rules/new", "/history", "/faq"):
            response = client.get(path)
            assert response.status_code == 200, f"{path} status {response.status_code}"
            assert 'id="wb-confirm-dialog"' in response.text
            assert "window.wbConfirm" in response.text


def test_rules_delete_form_uses_wbconfirm_handler(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    _seed_active_rule(postgres_engine, operator_id=op_id)
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/rules")

    assert response.status_code == 200
    # No stray native confirm() calls left in the rules list.
    assert "return confirm(" not in response.text
    assert "wbConfirm(this, event, 'Delete this rule?')" in response.text
