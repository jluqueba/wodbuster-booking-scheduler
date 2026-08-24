"""Component tests for the single-day override routes (T-BDO-006).

Runs against real Postgres and the real app, because the guards under
test are route-level: the ownership resolution (CC-013), the CSRF
dependency, the server-side cutoff re-check (CC-003) and the published
combination check (CC-004). Mocking any of them would test the mock.

``WORKER_TIMEZONE`` is pinned to Europe/Madrid rather than UTC so the
operator-local day arithmetic is exercised rather than reduced to an
identity, and every clock read goes through the frozen ``_utcnow`` seam
so no assertion depends on the week the suite happens to run in.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, date, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.persistence.models import BookingDayOverride, SchedulerRule
from wodbuster_worker.security.cipher import Cipher
from wodbuster_worker.wodbuster_client.client import LoadClassResponse

from .conftest import gym_account_id_for

# Class on Wednesday 6 May 2026 at 18:30 Madrid. The window opens two
# days before at 21:30 Madrid (19:30 UTC), so the edit cutoff is
# 19:28:30 UTC on Monday 4 May.
TARGET_DATE = date(2026, 5, 6)
BEFORE_CUTOFF = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
AFTER_CUTOFF = datetime(2026, 5, 4, 19, 29, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _madrid_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@pytest.fixture
def freeze_now(monkeypatch: pytest.MonkeyPatch) -> Callable[[datetime], None]:
    """Freeze the clock both routes read (the history module's seam)."""

    def _freeze(instant: datetime) -> None:
        monkeypatch.setattr("wodbuster_worker.booking.override_routes._utcnow", lambda: instant)
        monkeypatch.setattr("wodbuster_worker.booking.routes._utcnow", lambda: instant)

    return _freeze


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


def _csrf(client: TestClient) -> dict[str, str]:
    token = client.cookies.get("wodbuster_csrf")
    assert token, "expected wodbuster_csrf cookie after sign-in"
    return {"X-CSRF-Token": token}


def _seed_rule(engine: Engine, operator_id: int, *, class_type: str = "WOD") -> tuple[int, int]:
    """Insert a Wednesday 18:30 rule. Returns ``(rule_id, gym_account_id)``."""
    with engine.begin() as conn:
        gym_account_id = gym_account_id_for(conn, operator_id)
        rule_id = int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule "
                    "(gym_account_id, day_of_week, class_type, class_time, "
                    " booking_opens_days_before, booking_opens_at, active) "
                    "VALUES (:ga, 2, :ct, '18:30', 2, '21:30', true) RETURNING id"
                ),
                {"ga": gym_account_id, "ct": class_type},
            ).scalar_one()
        )
    return rule_id, gym_account_id


def _url(rule_id: int, *, revert: bool = False) -> str:
    base = f"/history/overrides/{rule_id}/{TARGET_DATE.isoformat()}"
    return f"{base}/revert" if revert else base


class _FakeClient:
    """LoadClass stub: one scripted payload for every ticks value."""

    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload
        self.calls: list[int] = []

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        _ = cookie_value
        self.calls.append(ticks)
        return LoadClassResponse(status_code=200, latency_ms=5.0, payload=self._payload)


def _payload_with(*pairs: tuple[str, str]) -> dict[str, Any]:
    """Build a LoadClass payload carrying one class instance per pair."""
    return {
        "Data": [
            {
                "Hora": f"{class_time}:00",
                "Valores": [
                    {
                        "Valor": {
                            "Id": 1000 + index,
                            "Nombre": class_type,
                            "HoraComienzo": f"{class_time}:00",
                            "TipoEstado": "Inscribible",
                            "Plazas": 12,
                            "AtletasEnListaDeEspera": 0,
                        }
                    }
                ],
            }
            for index, (class_type, class_time) in enumerate(pairs)
        ]
    }


def _wire_probe(
    app: FastAPI,
    engine: Engine,
    gym_account_id: int,
    payload: dict[str, Any],
) -> _FakeClient:
    """Attach a cookie store with a live cookie plus a scripted client."""
    store = CookieStore(Cipher(os.urandom(32)))
    factory = sessionmaker(bind=engine)
    with factory() as session:
        store.save(session, gym_account_id, ".WBAuth-tok", validated_at=datetime.now(tz=UTC))
        session.commit()
    fake = _FakeClient(payload)
    app.state.cookie_store = store
    app.state.wodbuster_client = fake
    return fake


def _overrides(session_factory: sessionmaker[Session]) -> list[BookingDayOverride]:
    with session_factory() as session:
        return list(session.execute(select(BookingDayOverride)).scalars().all())


# ---------------------------------------------------------------------------
# Security controls
# ---------------------------------------------------------------------------


def test_requires_active_gym_account(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    freeze_now: Callable[[datetime], None],
) -> None:
    """No gym account on the session means no route answers (ADR-0007)."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        headers = _csrf(client)
        # Deactivated after sign-in so the switcher resolves to nothing
        # on the next request, which is the state the routes must refuse.
        with postgres_engine.begin() as conn:
            conn.execute(
                text("UPDATE gym_account SET active = false WHERE user_id = :op"),
                {"op": op_id},
            )
        assert client.get(_url(rule_id)).status_code == 404
        assert client.post(_url(rule_id), data={}, headers=headers).status_code == 404
        assert client.post(_url(rule_id, revert=True), headers=headers).status_code == 404


def test_foreign_rule_returns_404_indistinguishable_from_missing(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    freeze_now: Callable[[datetime], None],
) -> None:
    """CC-013: a foreign rule is not distinguishable from a missing one."""
    freeze_now(BEFORE_CUTOFF)
    _, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    op_b, _ = seed_operator(provider="microsoft", display_name="Bob")
    bob_rule_id, _ = _seed_rule(postgres_engine, op_b, class_type="BobsSecretClass")

    app = app_factory()
    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        foreign = client.get(_url(bob_rule_id))
        missing = client.get(_url(bob_rule_id + 10_000))

    assert foreign.status_code == 404
    assert missing.status_code == 404
    assert foreign.content == missing.content
    assert "BobsSecretClass" not in foreign.text


def test_save_without_csrf_rejected(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(_url(rule_id), data={"class_type": "WOD", "class_time": "19:00"})

    assert response.status_code == 403
    assert _overrides(session_factory) == []


def test_revert_without_csrf_rejected(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    freeze_now: Callable[[datetime], None],
) -> None:
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(_url(rule_id, revert=True))

    assert response.status_code == 403


def test_gym_account_derived_from_rule(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """A body-supplied gym account is ignored; the rule's own is written."""
    freeze_now(BEFORE_CUTOFF)
    op_a, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    op_b, _ = seed_operator(provider="microsoft", display_name="Bob")
    rule_id, alice_gym = _seed_rule(postgres_engine, op_a)
    with postgres_engine.begin() as conn:
        bob_gym = gym_account_id_for(conn, op_b)

    app = app_factory()
    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        response = client.post(
            _url(rule_id),
            data={
                "class_type": "WOD",
                "class_time": "19:00",
                "gym_account_id": str(bob_gym),
            },
            headers=_csrf(client),
        )

    assert response.status_code == 303
    rows = _overrides(session_factory)
    assert len(rows) == 1
    assert rows[0].gym_account_id == alice_gym


def test_late_save_rejected(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """CC-003: past the cutoff nothing is written and the user is told."""
    freeze_now(AFTER_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            _url(rule_id),
            data={"class_type": "WOD", "class_time": "19:00"},
            headers=_csrf(client),
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/history?")
    assert "already+opened" in response.headers["location"]
    assert _overrides(session_factory) == []


def test_late_revert_rejected(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """The cutoff is re-checked on revert too, not only on save."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)
    with postgres_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO booking_day_override "
                "(rule_id, gym_account_id, target_date, class_time) "
                "VALUES (:r, :ga, :d, '19:00')"
            ),
            {"r": rule_id, "ga": gym_account_id, "d": TARGET_DATE},
        )

    freeze_now(AFTER_CUTOFF)
    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(_url(rule_id, revert=True), headers=_csrf(client))

    assert response.status_code == 303
    assert "already+opened" in response.headers["location"]
    assert len(_overrides(session_factory)) == 1


# ---------------------------------------------------------------------------
# The form
# ---------------------------------------------------------------------------


def test_form_renders_real_pairs_for_a_published_date(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    freeze_now: Callable[[datetime], None],
) -> None:
    """CC-004 half: the selectors carry the date's own combinations."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        _wire_probe(
            app,
            postgres_engine,
            gym_account_id,
            _payload_with(("WOD", "18:30"), ("Endurance", "19:00")),
        )
        response = client.get(_url(rule_id))

    assert response.status_code == 200
    assert "Endurance" in response.text
    assert "19:00" in response.text


def test_form_404_once_the_cutoff_has_passed(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    freeze_now: Callable[[datetime], None],
) -> None:
    """FR-005/FR-006: there is nothing to offer past the cutoff."""
    freeze_now(AFTER_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get(_url(rule_id))

    assert response.status_code == 404


def test_form_404_when_the_rule_does_not_project_that_date(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    freeze_now: Callable[[datetime], None],
) -> None:
    """A Wednesday rule has nothing to say about a Thursday."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get(f"/history/overrides/{rule_id}/2026-05-07")

    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Save
# ---------------------------------------------------------------------------


def test_save_class_time_change_is_validated_against_the_date(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """CC-001 (save half): a confirmed pair persists ``validated=true``."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        _wire_probe(
            app,
            postgres_engine,
            gym_account_id,
            _payload_with(("WOD", "18:30"), ("WOD", "19:00")),
        )
        response = client.post(
            _url(rule_id),
            data={"class_type": "WOD", "class_time": "19:00"},
            headers=_csrf(client),
        )

    assert response.status_code == 303
    rows = _overrides(session_factory)
    assert len(rows) == 1
    assert rows[0].class_time == "19:00"
    # Untouched dimension stays NULL: the row records the change, not a
    # copy of the rule.
    assert rows[0].class_type is None
    assert rows[0].validated is True

    # The rule itself is unchanged (INV-003).
    with session_factory() as session:
        rule = session.get(SchedulerRule, rule_id)
        assert rule is not None
        assert rule.class_time == "18:30"
        assert rule.class_type == "WOD"


def test_save_blocked_when_published_schedule_lacks_the_pair(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """CC-004: driven by a direct POST, since the selectors are not the
    enforcement point."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        _wire_probe(
            app,
            postgres_engine,
            gym_account_id,
            # Endurance runs, but at 10:00, not at 07:00.
            _payload_with(("WOD", "18:30"), ("Endurance", "10:00")),
        )
        response = client.post(
            _url(rule_id),
            data={"class_type": "Endurance", "class_time": "07:00"},
            headers=_csrf(client),
        )

    assert response.status_code == 422
    assert "does not run at that time" in response.text
    assert _overrides(session_factory) == []


def test_save_against_unpublished_schedule_is_not_validated(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """CC-005: an unpublished date saves, marked not validated."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        _wire_probe(app, postgres_engine, gym_account_id, {"Data": []})
        response = client.post(
            _url(rule_id),
            data={"class_type": "Endurance", "class_time": "07:00"},
            headers=_csrf(client),
        )
        form = client.get(_url(rule_id))

    assert response.status_code == 303
    rows = _overrides(session_factory)
    assert len(rows) == 1
    assert rows[0].validated is False
    # The warning names the unpublished schedule, not the cookie state.
    assert "has not published the schedule" in form.text


def test_save_without_a_probe_is_not_validated_and_warns_about_the_cookie(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """FR-009: no probe answer still saves, with the distinct warning."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            _url(rule_id),
            data={"class_type": "Endurance", "class_time": "07:00"},
            headers=_csrf(client),
        )
        form = client.get(_url(rule_id))

    assert response.status_code == 303
    rows = _overrides(session_factory)
    assert len(rows) == 1
    assert rows[0].validated is False
    assert "Live class list unavailable" in form.text


def test_duplicate_submission_produces_one_row(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """INV-001: the second submission updates rather than inserting."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        payload = {"class_type": "WOD", "class_time": "19:00"}
        first = client.post(_url(rule_id), data=payload, headers=_csrf(client))
        second = client.post(_url(rule_id), data=payload, headers=_csrf(client))

    assert first.status_code == 303
    assert second.status_code == 303
    assert len(_overrides(session_factory)) == 1


def test_save_of_the_rules_own_values_reverts_the_day(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """An override with no effect is what the user means by "back to the rule"."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        headers = _csrf(client)
        client.post(
            _url(rule_id), data={"class_type": "WOD", "class_time": "19:00"}, headers=headers
        )
        response = client.post(
            _url(rule_id), data={"class_type": "WOD", "class_time": "18:30"}, headers=headers
        )

    assert response.status_code == 303
    assert _overrides(session_factory) == []


def test_save_rejects_a_malformed_time(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            _url(rule_id),
            data={"class_type": "WOD", "class_time": "25:99"},
            headers=_csrf(client),
        )

    assert response.status_code == 422
    assert _overrides(session_factory) == []


# ---------------------------------------------------------------------------
# Revert
# ---------------------------------------------------------------------------


def test_revert_deletes_the_row_and_redirects(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
    session_factory: sessionmaker[Session],
    freeze_now: Callable[[datetime], None],
) -> None:
    """CC-015, including the idempotent second submission."""
    freeze_now(BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        headers = _csrf(client)
        client.post(
            _url(rule_id), data={"class_type": "WOD", "class_time": "19:00"}, headers=headers
        )
        first = client.post(_url(rule_id, revert=True), headers=headers)
        second = client.post(_url(rule_id, revert=True), headers=headers)

    assert first.status_code == 303
    assert first.headers["location"].startswith("/history?")
    assert second.status_code == 303
    assert _overrides(session_factory) == []
