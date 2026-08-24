"""Every rendered page must satisfy the accessibility invariants.

It renders each screen through the same test client the rest of the
suite uses, parses the response with a spec-conformant HTML5 parser, and
asserts the invariants documented in :mod:`tests.component.a11y`. No
browser, no network, so it runs on every commit.

Coverage is therefore the invariants written down here, not everything a
full audit engine would flag. Running axe against a real browser was
considered and dropped: it would put a browser download in every CI run
to re-check pages this sweep already walks.

Both languages are swept. That is not symmetry for its own sake: a
confirmation prompt was once silently broken in English only, because
the English copy carried an apostrophe and the Spanish copy did not. A
single-language sweep would have missed it, and translated copy is
exactly where an empty button label or a missing image description
appears on one branch and not the other.

``/cookie`` is excluded because the test app is built without the cookie
stack and the route answers 503 before rendering anything, the same
reason ``test_ui_polish`` skips it.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from datetime import UTC, date, datetime, timedelta
from typing import Any, NamedTuple

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from starlette.responses import RedirectResponse

from wodbuster_worker.notifications.unsubscribe import make_unsubscribe_token

from .a11y import audit
from .conftest import gym_account_id_for

# A Wednesday class, and an instant two days before it while the edit
# window is still open, so the override form renders its editable state
# rather than a 404. Frozen for the same reason the override route tests
# freeze: otherwise the page under audit changes with the calendar week.
TARGET_DATE = date(2026, 5, 6)
FROZEN_NOW = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)

LANGUAGES = [("en", ""), ("es", "/es")]


class Seed(NamedTuple):
    operator_id: int
    subject_id: str
    rule_id: int


def _report(problems: list[str]) -> None:
    """Fail once with the whole sweep rather than page by page.

    Stopping at the first page would turn a single broken partial,
    which is included everywhere, into one fix-and-rerun cycle per
    screen.
    """
    assert not problems, "accessibility invariants broken:\n  " + "\n  ".join(problems)


@pytest.fixture(autouse=True)
def _madrid_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin the gym timezone so rendered local times do not drift."""
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")


@pytest.fixture(autouse=True)
def _frozen_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("wodbuster_worker.booking.routes._utcnow", lambda: FROZEN_NOW)
    monkeypatch.setattr("wodbuster_worker.booking.override_routes._utcnow", lambda: FROZEN_NOW)


def _sign_in(
    app: FastAPI,
    subject_id: str,
    display_name: str,
    monkeypatch: pytest.MonkeyPatch,
    *,
    prefix: str = "",
) -> TestClient:
    """Return a client holding a seated session, entered on ``prefix``."""
    client = app.state.oauth.create_client("microsoft")

    async def fake_authorize_access_token(_request: Any) -> dict[str, Any]:
        return {
            "userinfo": {"sub": subject_id, "name": display_name},
            "access_token": "fake-token",
        }

    async def fake_authorize_redirect(_request: Any, _redirect_uri: str, **_kw: Any) -> Any:
        return RedirectResponse("https://provider/authorize", status_code=302)

    monkeypatch.setattr(client, "authorize_access_token", fake_authorize_access_token)
    monkeypatch.setattr(client, "authorize_redirect", fake_authorize_redirect)
    tc = TestClient(app, follow_redirects=False)
    tc.get(f"{prefix}/auth/microsoft/login")
    response = tc.get("/auth/microsoft/callback?code=fake&state=fake")
    assert response.status_code == 302, response.text
    return tc


@pytest.fixture
def seeded(
    postgres_engine: Engine,
    seed_operator: Callable[..., tuple[int, str]],
) -> Seed:
    """Populate every table the swept pages read a row from.

    Empty pages hide the markup that carries most of the risk: table
    headers, row action buttons and the controls inside them only exist
    once there is something to list.
    """
    operator_id, subject_id = seed_operator(provider="microsoft", display_name="Alice Operator")
    real_now = datetime.now(tz=UTC)
    with postgres_engine.begin() as conn:
        gym_account_id = gym_account_id_for(conn, operator_id)
        conn.execute(
            text(
                "UPDATE operator_profile "
                "SET is_admin = true, email = :e, telegram_chat_id = '4242' "
                "WHERE id = :i"
            ),
            {"i": operator_id, "e": "alice@example.test"},
        )
        rule_id = int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule "
                    "(gym_account_id, day_of_week, class_type, class_time, "
                    " second_shot_class_type, second_shot_class_time, "
                    " booking_opens_days_before, booking_opens_at, active) "
                    "VALUES (:ga, 2, 'WOD', '18:30', 'Open Box', '19:30', 2, '21:30', true) "
                    "RETURNING id"
                ),
                {"ga": gym_account_id},
            ).scalar_one()
        )
        # One outcome per status family so the history table renders every
        # chip and every row action, not just the happy path.
        for status, source, offset in (
            ("granted", "rule", timedelta(days=-1)),
            ("full", "override_fallback", timedelta(days=-2)),
            ("skipped", "override_skip", timedelta(days=-3)),
        ):
            conn.execute(
                text(
                    "INSERT INTO booking_outcome "
                    "(gym_account_id, target_class, target_slot, terminal_status, "
                    " outcome_source, attempted_at) "
                    "VALUES (:ga, 'WOD', :slot, :status, :source, :at)"
                ),
                {
                    "ga": gym_account_id,
                    "slot": FROZEN_NOW + offset + timedelta(days=4),
                    "status": status,
                    "source": source,
                    "at": FROZEN_NOW + offset,
                },
            )
        conn.execute(
            text(
                "INSERT INTO vacation_window (gym_account_id, start_date, end_date) "
                "VALUES (:ga, :s, :e)"
            ),
            {
                "ga": gym_account_id,
                "s": real_now - timedelta(days=1),
                "e": real_now + timedelta(days=5),
            },
        )
        # A pending signup and a second active user so the admin page
        # renders both of its tables with their action controls.
        for display_name, status in (("Pending Pat", "pending"), ("Active Alex", "active")):
            other_id = conn.execute(
                text(
                    "INSERT INTO operator_profile (display_name, status, email) "
                    "VALUES (:n, :s, :e) RETURNING id"
                ),
                {"n": display_name, "s": status, "e": f"{status}@example.test"},
            ).scalar_one()
            conn.execute(
                text(
                    "INSERT INTO federated_identity "
                    "(operator_id, provider, subject_id, display_name) "
                    "VALUES (:op, 'google', :s, :n)"
                ),
                {"op": other_id, "s": f"sub-{status}", "n": display_name},
            )
    return Seed(operator_id=operator_id, subject_id=subject_id, rule_id=rule_id)


def _authenticated_pages(rule_id: int) -> list[tuple[str, str]]:
    return [
        ("dashboard", "/"),
        ("rules list", "/rules"),
        ("rule create", "/rules/new"),
        ("rule edit", f"/rules/{rule_id}"),
        ("history", "/history"),
        ("override form", f"/history/overrides/{rule_id}/{TARGET_DATE.isoformat()}"),
        ("vacation", "/vacation"),
        ("telegram", "/telegram"),
        ("profile", "/profile"),
        ("faq", "/faq"),
        ("admin users", "/admin/users"),
    ]


@pytest.mark.parametrize(("lang", "prefix"), LANGUAGES, ids=["en", "es"])
def test_authenticated_pages_are_accessible(
    app_factory: Callable[..., FastAPI],
    seeded: Seed,
    monkeypatch: pytest.MonkeyPatch,
    lang: str,
    prefix: str,
) -> None:
    app = app_factory()
    problems: list[str] = []
    with _sign_in(app, seeded.subject_id, "Alice Operator", monkeypatch, prefix=prefix) as client:
        for page, path in _authenticated_pages(seeded.rule_id):
            response = client.get(f"{prefix}{path}")
            assert response.status_code == 200, f"{page} ({prefix}{path}) -> {response.status_code}"
            problems += [
                f"{page} [{lang}] {prefix}{path}: {problem}"
                for problem in audit(response.text, expected_lang=lang)
            ]
    _report(problems)


@pytest.mark.parametrize(("lang", "prefix"), LANGUAGES, ids=["en", "es"])
def test_public_pages_are_accessible(
    app_factory: Callable[..., FastAPI],
    postgres_engine: Engine,
    seed_operator: Callable[..., tuple[int, str]],
    lang: str,
    prefix: str,
) -> None:
    """Sweep the pages a visitor can reach without a session."""
    app = app_factory()
    operator_id, _ = seed_operator(provider="microsoft", display_name="Alice Operator")
    problems: list[str] = []

    with TestClient(app, follow_redirects=False) as client:
        # The unsubscribe signing secret is seated by the lifespan, so
        # the token can only be minted once the client has started up.
        token = make_unsubscribe_token(operator_id, secret=app.state.email_unsubscribe_secret)
        pages = [
            ("landing", "/", 200),
            ("suspended", "/auth/suspended", 200),
            ("unsubscribe accepted", f"/unsubscribe?t={token}", 200),
            ("unsubscribe rejected", "/unsubscribe?t=not-a-token", 400),
        ]
        for page, path, status in pages:
            response = client.get(f"{prefix}{path}")
            assert response.status_code == status, f"{page} -> {response.status_code}"
            problems += [
                f"{page} [{lang}] {prefix}{path}: {problem}"
                for problem in audit(response.text, expected_lang=lang)
            ]
    _report(problems)


def _oauth_outcome_pages(
    app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
    prefix: str,
) -> Iterator[tuple[str, str]]:
    """Yield ``(page, body)`` for the two pages only the callback renders.

    Neither the pending nor the denial page has a URL of its own: the
    OAuth callback decides which one the visitor gets. Driving the
    callback is the only way to see the markup a rejected or unapproved
    visitor actually receives.
    """
    from authlib.integrations.base_client.errors import OAuthError

    client = app.state.oauth.create_client("microsoft")

    async def fake_authorize_redirect(_request: Any, _redirect_uri: str, **_kw: Any) -> Any:
        return RedirectResponse("https://provider/authorize", status_code=302)

    monkeypatch.setattr(client, "authorize_redirect", fake_authorize_redirect)

    async def unknown_identity(_request: Any) -> dict[str, Any]:
        return {"userinfo": {"sub": "unknown-subject", "name": "Nobody"}, "access_token": "t"}

    async def failed_handshake(_request: Any) -> dict[str, Any]:
        raise OAuthError(description="denied")

    for page, token_call in (("pending", unknown_identity), ("denied", failed_handshake)):
        monkeypatch.setattr(client, "authorize_access_token", token_call)
        with TestClient(app, follow_redirects=False) as http:
            # The callback URL carries no language prefix, so the visitor's
            # language is the one stored when they started the login.
            http.get(f"{prefix}/auth/microsoft/login")
            response = http.get("/auth/microsoft/callback?code=fake&state=fake")
        assert response.status_code in {200, 403}, f"{page} -> {response.status_code}"
        yield page, response.text


@pytest.mark.parametrize(("lang", "prefix"), LANGUAGES, ids=["en", "es"])
def test_oauth_outcome_pages_are_accessible(
    app_factory: Callable[..., FastAPI],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    lang: str,
    prefix: str,
) -> None:
    app = app_factory()
    problems: list[str] = []
    for page, body in _oauth_outcome_pages(app, monkeypatch, prefix):
        problems += [f"{page} [{lang}]: {problem}" for problem in audit(body, expected_lang=lang)]
    _report(problems)
