"""Component tests for /history + /bookings/{id}/cancel (US6.2, H.1 lite).

Covers:

- Empty state: operator with no bookings sees the "no bookings yet"
  copy.
- Row rendering: an operator sees their own bookings, with a Cancel
  button on granted+future rows only.
- Isolation (CC-012): Alice's history never shows Bob's outcomes.
- Cancel happy path: POST /bookings/{id}/cancel invokes borrar,
  flips the row to ``cancelled``, and enqueues a banner outbox.
- Cancel idempotency (CC-015): a second POST is a no-op.
- Cancel cross-operator: POST for a row owned by someone else 404s.
- Cancel with no wodbuster client wired: friendly error flash.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from wodbuster_worker.persistence.models import BookingOutcome, NotificationOutbox
from wodbuster_worker.wodbuster_client.client import BookingActionResponse, LoadClassResponse

from .conftest import gym_account_id_for


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


def _seed_booking(
    engine: Engine,
    *,
    operator_id: int,
    target_class: str = "WOD",
    target_slot: datetime | None = None,
    terminal_status: str = "granted",
    attempted_at: datetime | None = None,
) -> int:
    """Insert a booking outcome directly. Returns the row id."""
    if target_slot is None:
        target_slot = datetime.now(tz=UTC) + timedelta(days=3)
    columns = "(gym_account_id, target_class, target_slot, terminal_status"
    values = "(:ga, :cls, :slot, :status"
    params: dict[str, Any] = {
        "cls": target_class,
        "slot": target_slot,
        "status": terminal_status,
    }
    if attempted_at is not None:
        columns += ", attempted_at"
        values += ", :attempted"
        params["attempted"] = attempted_at
    columns += ")"
    values += ")"
    with engine.begin() as conn:
        params["ga"] = gym_account_id_for(conn, operator_id)
        return int(
            conn.execute(
                text(f"INSERT INTO booking_outcome {columns} VALUES {values} RETURNING id"),
                params,
            ).scalar_one()
        )


class _FakeWodBusterClient:
    """Stub WodBuster client that scripts load_class + borrar responses."""

    def __init__(
        self,
        *,
        load_response: LoadClassResponse | Exception | None = None,
        borrar_response: BookingActionResponse | Exception | None = None,
    ) -> None:
        self._load_response = load_response
        self._borrar_response = borrar_response
        self.load_calls: list[dict[str, Any]] = []
        self.borrar_calls: list[dict[str, Any]] = []

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        self.load_calls.append({"cookie": cookie_value, "ticks": ticks})
        if isinstance(self._load_response, Exception):
            raise self._load_response
        if self._load_response is None:
            raise AssertionError("fake: no load_class response scripted")
        return self._load_response

    def borrar(
        self, cookie_value: str, *, class_id: str | int, ticks: int
    ) -> BookingActionResponse:
        self.borrar_calls.append({"cookie": cookie_value, "class_id": class_id, "ticks": ticks})
        if isinstance(self._borrar_response, Exception):
            raise self._borrar_response
        if self._borrar_response is None:
            raise AssertionError("fake: no borrar response scripted")
        return self._borrar_response


def _load_response_with(class_type: str, class_time: str) -> LoadClassResponse:
    """Build a LoadClass payload containing one matching class instance."""
    return LoadClassResponse(
        status_code=200,
        latency_ms=10.0,
        payload={
            "Data": [
                {
                    "Hora": f"{class_time}:00",
                    "Valores": [
                        {
                            "Valor": {
                                "Id": 45654,
                                "Nombre": class_type,
                                "HoraComienzo": f"{class_time}:00",
                                "TipoEstado": "Borrable",
                                "Plazas": 16,
                                "AtletasEnListaDeEspera": 0,
                            }
                        }
                    ],
                }
            ],
            "SegundosHastaPublicacion": -100.0,
        },
    )


def _borrar_ok() -> BookingActionResponse:
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="granted",
        raw_res="Ok",
        payload={"Res": "Ok"},
    )


# ---------------------------------------------------------------------------
# GET /history
# ---------------------------------------------------------------------------


def test_history_empty_state(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, subject = seed_operator(provider="microsoft", display_name="Alice")
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert "No attempts this week" in response.text


def test_history_lists_own_bookings_with_cancel_button_on_granted_future(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    granted_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        target_slot=datetime.now(tz=UTC) + timedelta(days=3),
        terminal_status="granted",
    )
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="Halterofilia",
        target_slot=datetime.now(tz=UTC) - timedelta(days=1),  # past
        terminal_status="granted",
    )
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="Cross Training",
        terminal_status="full",
    )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    # All three rows visible.
    assert "WOD" in response.text
    assert "Halterofilia" in response.text
    assert "Cross Training" in response.text
    # Cancel button only on the granted+future row.
    assert f'action="/bookings/{granted_id}/cancel"' in response.text
    # Past-granted and full rows do NOT get a cancel form. The granted
    # future row does — once in the upcoming grid, once in the full
    # attempts table below.
    assert response.text.count(f"/bookings/{granted_id}/cancel") == 2


def test_history_isolates_by_operator(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_a, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    op_b, _ = seed_operator(provider="microsoft", display_name="Bob")
    _seed_booking(postgres_engine, operator_id=op_b, target_class="BobsSecretClass")

    app = app_factory()
    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert "BobsSecretClass" not in response.text
    # Alice has no bookings → empty state.
    assert "No attempts this week" in response.text
    _ = op_a  # unused but binds the fixture return


def test_history_attempts_table_shows_only_current_week(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attempts table is scoped to the current week so it can't
    grow unbounded. An attempt made last week is filtered out; one
    made this week shows."""
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")

    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="LastWeekClass",
        target_slot=datetime.now(tz=UTC) - timedelta(days=10),
        terminal_status="granted",
        attempted_at=datetime.now(tz=UTC) - timedelta(days=10),
    )
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="ThisWeekClass",
        target_slot=datetime.now(tz=UTC) + timedelta(days=2),
        terminal_status="granted",
        attempted_at=datetime.now(tz=UTC),
    )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert "ThisWeekClass" in response.text
    assert "LastWeekClass" not in response.text


def test_history_attempts_table_renders_operator_local_time(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Attempt times are shown in the operator's zone (WORKER_TIMEZONE),
    like the rest of the app, not in UTC. A slot stored at 19:30 UTC
    renders as 21:30 in Europe/Madrid (CEST, UTC+2 in July)."""
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")
    # Freeze the route clock to a fixed instant in the same week as the
    # seeded slot. The attempts table is week-scoped, and the assertions
    # below rely on Europe/Madrid summer time (CEST, +2), so "now" must be
    # pinned to a July date. Without this the test would fail every week
    # outside 2026-07-13..19 and on every winter run.
    monkeypatch.setattr(
        "wodbuster_worker.booking.routes._utcnow",
        lambda: datetime(2026, 7, 16, 12, 0, tzinfo=UTC),
    )
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 15, 19, 30, tzinfo=UTC),
        terminal_status="granted",
        attempted_at=datetime(2026, 7, 15, 19, 35, tzinfo=UTC),
    )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    body = response.text
    # Slot and attempt both shift +2h into local time; nothing renders UTC.
    assert "21:30" in body
    assert "21:35" in body
    # The Day/Date columns show the weekday name and a combined
    # "date at time" label (15 Jul 2026 is a Wednesday).
    assert "Wednesday" in body
    assert "15 Jul at 21:30" in body
    assert "UTC" not in body


def test_history_upcoming_section_groups_future_granted_bookings(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """H.1 full: the ``🗓️ Upcoming bookings`` section lists granted
    bookings whose class start is in the future, grouped by day.
    Past-granted and non-granted rows do not appear in that section
    (they still show up in the ``📜 This week's attempts`` table below)."""
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")

    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="UpcomingWOD",
        target_slot=datetime.now(tz=UTC) + timedelta(days=2),
        terminal_status="granted",
    )
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="PastGranted",
        target_slot=datetime.now(tz=UTC) - timedelta(days=1),
        terminal_status="granted",
    )
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="FullClass",
        target_slot=datetime.now(tz=UTC) + timedelta(days=2),
        terminal_status="full",
    )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    text = response.text
    section_start = text.index('<section class="wb-upcoming">')
    section_end = text.index("</section>", section_start)
    upcoming = text[section_start:section_end]
    past_and_below = text[section_end:]

    # Only the future-granted class shows in the upcoming grid.
    assert "UpcomingWOD" in upcoming
    assert "PastGranted" not in upcoming
    assert "FullClass" not in upcoming
    # The full attempts table still lists every row.
    assert "UpcomingWOD" in past_and_below
    assert "PastGranted" in past_and_below
    assert "FullClass" in past_and_below


def test_history_upcoming_section_empty_state_when_nothing_future(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No future-granted bookings and no active rules → helper hint."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="PastGranted",
        target_slot=datetime.now(tz=UTC) - timedelta(days=1),
        terminal_status="granted",
    )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert "No granted or scheduled bookings on the horizon" in response.text


def test_history_upcoming_section_projects_pending_rule_attempts(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A rule whose next window has not fired yet appears as a
    ``⏱️ scheduled`` slot in the upcoming grid — that's the operator
    question "what am I about to book next?" the granted-only view
    could not answer."""
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")

    # Insert an active rule directly. Attendance day = today's
    # weekday + 1 mod 7 so the projection always lands in the
    # future regardless of when the suite runs.
    now = datetime.now(tz=UTC)
    attend_dow = (now.weekday() + 1) % 7
    with postgres_engine.begin() as conn:
        gym_account_id = gym_account_id_for(conn, op_id)
        conn.execute(
            text(
                "INSERT INTO scheduler_rule ("
                " gym_account_id, day_of_week, class_type, class_time, "
                " booking_opens_days_before, booking_opens_at, active"
                ") VALUES (:ga, :dow, 'ScheduledWOD', '21:30', 0, '21:30', true)"
            ),
            {"ga": gym_account_id, "dow": attend_dow},
        )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    text_body = response.text
    section_start = text_body.index('<section class="wb-upcoming">')
    section_end = text_body.index("</section>", section_start)
    upcoming = text_body[section_start:section_end]
    assert "ScheduledWOD" in upcoming
    assert "scheduled" in upcoming  # pending chip label


def test_history_upcoming_section_marks_vacation_covered_slots(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A projected slot inside an open vacation window shows an
    ``🏖️ on vacation`` chip (muted, struck through) instead of the
    ``scheduled`` chip, so the operator sees the class will be skipped."""
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")

    now = datetime.now(tz=UTC)
    attend_dow = (now.weekday() + 1) % 7
    with postgres_engine.begin() as conn:
        gym_account_id = gym_account_id_for(conn, op_id)
        conn.execute(
            text(
                "INSERT INTO scheduler_rule ("
                " gym_account_id, day_of_week, class_type, class_time, "
                " booking_opens_days_before, booking_opens_at, active"
                ") VALUES (:ga, :dow, 'VacayWOD', '21:30', 0, '21:30', true)"
            ),
            {"ga": gym_account_id, "dow": attend_dow},
        )
        # An open vacation window covering the whole projection horizon.
        conn.execute(
            text(
                "INSERT INTO vacation_window ("
                " gym_account_id, start_date, end_date, created_at"
                ") VALUES (:ga, :s, :e, :c)"
            ),
            {
                "ga": gym_account_id,
                "s": now - timedelta(days=1),
                "e": now + timedelta(days=20),
                "c": now - timedelta(days=1),
            },
        )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    text_body = response.text
    section_start = text_body.index('<section class="wb-upcoming">')
    section_end = text_body.index("</section>", section_start)
    upcoming = text_body[section_start:section_end]
    assert "VacayWOD" in upcoming
    assert "on vacation" in upcoming  # vacation chip label
    assert "wb-upcoming__item--vacation" in upcoming
    # Every projected slot is covered, so no "scheduled" chip remains.
    assert "scheduled" not in upcoming


def test_history_unauthenticated_redirects_to_login(
    app_factory: Callable[..., FastAPI],
) -> None:
    app = app_factory()
    with TestClient(app, follow_redirects=False) as client:
        response = client.get("/history")
    assert response.status_code == 302
    assert "/auth/" in response.headers["location"]


# ---------------------------------------------------------------------------
# POST /bookings/{id}/cancel
# ---------------------------------------------------------------------------


def test_cancel_granted_booking_flips_row_and_enqueues_outbox(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # WodBuster reports class times in the gym's local wall clock. A 21:30
    # Europe/Madrid class (CEST, +2 in July) is stored as 19:30 UTC, so the
    # cancel path must convert target_slot back to local before matching.
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    booking_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 15, 19, 30, tzinfo=UTC),
    )

    fake_client = _FakeWodBusterClient(
        load_response=_load_response_with("WOD", "21:30"),
        borrar_response=_borrar_ok(),
    )

    app = app_factory()
    # Cookie stack + fake client are seeded AFTER sign-in (which
    # triggers the lifespan) so the lifespan does not overwrite them.
    import os

    from wodbuster_worker.persistence.cookie_store import CookieStore
    from wodbuster_worker.security.cipher import Cipher

    cipher = Cipher(os.urandom(32))
    store = CookieStore(cipher)
    factory = sessionmaker(bind=postgres_engine)
    with factory() as session:
        store.save(session, op_id, ".WBAuth-tok", validated_at=datetime.now(tz=UTC))
        session.commit()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        app.state.cookie_store = store
        app.state.wodbuster_client = fake_client
        response = client.post(
            f"/bookings/{booking_id}/cancel",
            data={"_csrf": client.cookies["wodbuster_csrf"]},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/history?")
    assert "Booking+cancelled" in response.headers["location"]

    # WodBuster was called with the resolved class id.
    assert len(fake_client.load_calls) == 1
    assert len(fake_client.borrar_calls) == 1
    assert fake_client.borrar_calls[0]["class_id"] == 45654

    # Row now marked cancelled with a paired outbox row.
    with factory() as session:
        row = session.get(BookingOutcome, booking_id)
        assert row is not None
        assert row.terminal_status == "cancelled"
        outbox = session.query(NotificationOutbox).filter_by(user_id=op_id).all()
        # At least one banner row; the cancel path enqueues one banner
        # (Telegram only when chat_id is set — Alice has none in this test).
        kinds = [row.kind for row in outbox]
        assert "banner" in kinds


def test_cancel_already_cancelled_is_idempotent(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-015: a second cancel is a no-op — no WodBuster call issued."""
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    booking_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        terminal_status="cancelled",
    )

    fake_client = _FakeWodBusterClient()
    app = app_factory()
    import os

    from wodbuster_worker.persistence.cookie_store import CookieStore
    from wodbuster_worker.security.cipher import Cipher

    cipher = Cipher(os.urandom(32))
    store = CookieStore(cipher)

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        app.state.cookie_store = store
        app.state.wodbuster_client = fake_client
        response = client.post(
            f"/bookings/{booking_id}/cancel",
            data={"_csrf": client.cookies["wodbuster_csrf"]},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    assert "Already+cancelled" in response.headers["location"]
    assert fake_client.load_calls == []
    assert fake_client.borrar_calls == []


def test_cancel_cross_operator_returns_404(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _op_a, subject_a = seed_operator(provider="microsoft", display_name="Alice")
    op_b, _ = seed_operator(provider="microsoft", display_name="Bob")
    bob_booking = _seed_booking(postgres_engine, operator_id=op_b, target_class="WOD")

    fake_client = _FakeWodBusterClient()
    app = app_factory()
    import os

    from wodbuster_worker.persistence.cookie_store import CookieStore
    from wodbuster_worker.security.cipher import Cipher

    cipher = Cipher(os.urandom(32))

    with _sign_in(app, subject_a, "Alice", monkeypatch) as client:
        app.state.cookie_store = CookieStore(cipher)
        app.state.wodbuster_client = fake_client
        response = client.post(
            f"/bookings/{bob_booking}/cancel",
            data={"_csrf": client.cookies["wodbuster_csrf"]},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 404
    assert fake_client.load_calls == []


def test_cancel_without_wodbuster_stack_returns_friendly_error(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    booking_id = _seed_booking(postgres_engine, operator_id=op_id)

    app = app_factory()
    # Deliberately do NOT wire wodbuster_client / cookie_store.
    assert app.state.wodbuster_client is None

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(
            f"/bookings/{booking_id}/cancel",
            data={"_csrf": client.cookies["wodbuster_csrf"]},
            headers=_csrf_headers(client),
        )

    assert response.status_code == 303
    assert "flash_kind=error" in response.headers["location"]


def test_cancel_without_csrf_is_forbidden(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    booking_id = _seed_booking(postgres_engine, operator_id=op_id)
    app = app_factory()

    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.post(f"/bookings/{booking_id}/cancel")

    assert response.status_code == 403


# ---------------------------------------------------------------------------
# Upcoming: the modified state and its actions (T-BDO-008)
# ---------------------------------------------------------------------------

# Wednesday 6 May 2026 at 18:30 Madrid. The window opens two days before
# at 21:30 Madrid (19:30 UTC), so the edit cutoff is 19:28:30 UTC on the
# Monday. Every override test below freezes ``_utcnow`` on one side of
# that instant rather than assuming anything about the current week.
_OVERRIDE_DATE = date(2026, 5, 6)
_BEFORE_CUTOFF = datetime(2026, 5, 4, 12, 0, tzinfo=UTC)
_AFTER_CUTOFF = datetime(2026, 5, 4, 19, 29, tzinfo=UTC)


def _seed_rule(engine: Engine, operator_id: int) -> tuple[int, int]:
    """Insert a Wednesday 18:30 WOD rule. Returns ``(rule_id, gym_account_id)``."""
    with engine.begin() as conn:
        gym_account_id = gym_account_id_for(conn, operator_id)
        rule_id = int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule "
                    "(gym_account_id, day_of_week, class_type, class_time, "
                    " booking_opens_days_before, booking_opens_at, active) "
                    "VALUES (:ga, 2, 'WOD', '18:30', 2, '21:30', true) RETURNING id"
                ),
                {"ga": gym_account_id},
            ).scalar_one()
        )
    return rule_id, gym_account_id


def _seed_override(
    engine: Engine,
    *,
    rule_id: int,
    gym_account_id: int,
    class_time: str | None = "19:00",
    skip_day: bool = False,
    validated: bool = True,
    target_date: date = _OVERRIDE_DATE,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO booking_day_override "
                "(rule_id, gym_account_id, target_date, class_time, skip_day, validated) "
                "VALUES (:r, :ga, :d, :ct, :skip, :v)"
            ),
            {
                "r": rule_id,
                "ga": gym_account_id,
                "d": target_date,
                "ct": None if skip_day else class_time,
                "skip": skip_day,
                "v": validated,
            },
        )


def _override_url(rule_id: int, *, revert: bool = False) -> str:
    base = f"/history/overrides/{rule_id}/{_OVERRIDE_DATE.isoformat()}"
    return f"{base}/revert" if revert else base


def _freeze(monkeypatch: pytest.MonkeyPatch, instant: datetime) -> None:
    monkeypatch.setenv("WORKER_TIMEZONE", "Europe/Madrid")
    monkeypatch.setattr("wodbuster_worker.booking.routes._utcnow", lambda: instant)


def test_history_renders_modified_day_with_rule_values_and_both_actions(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-019, FR-020, CC-001 (render half): the chip, the rule's own
    values as an annotation, and the edit + revert actions."""
    _freeze(monkeypatch, _BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)
    _seed_override(postgres_engine, rule_id=rule_id, gym_account_id=gym_account_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    body = response.text
    assert "wb-chip--modified" in body
    # Effective time, and the rule's own values annotated beside it.
    assert "19:00" in body
    assert "Rule: WOD at 18:30" in body
    assert _override_url(rule_id) in body
    assert _override_url(rule_id, revert=True) in body


def test_history_pending_day_offers_edit_but_no_revert(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """There is nothing to revert on a day that carries no override."""
    _freeze(monkeypatch, _BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, _ = _seed_rule(postgres_engine, op_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert _override_url(rule_id) in response.text
    assert _override_url(rule_id, revert=True) not in response.text


def test_history_granted_day_offers_neither_action(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A booked day is past editing (FR-005)."""
    _freeze(monkeypatch, _BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)
    with postgres_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO booking_outcome "
                "(gym_account_id, rule_id, target_class, target_slot, terminal_status) "
                "VALUES (:ga, :r, 'WOD', :slot, 'granted')"
            ),
            {
                "ga": gym_account_id,
                "r": rule_id,
                "slot": datetime(2026, 5, 6, 16, 30, tzinfo=UTC),
            },
        )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert _override_url(rule_id) not in response.text
    assert _override_url(rule_id, revert=True) not in response.text


def test_history_vacation_day_offers_neither_action(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-014: vacation wins, so the day is not editable either."""
    _freeze(monkeypatch, _BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)
    _seed_override(postgres_engine, rule_id=rule_id, gym_account_id=gym_account_id)
    with postgres_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO vacation_window (gym_account_id, start_date, end_date) "
                "VALUES (:ga, :start, :end)"
            ),
            {
                "ga": gym_account_id,
                "start": datetime(2026, 5, 5, 0, 0, tzinfo=UTC),
                "end": datetime(2026, 5, 7, 0, 0, tzinfo=UTC),
            },
        )

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    assert "wb-chip--vacation" in response.text
    assert _override_url(rule_id) not in response.text
    assert _override_url(rule_id, revert=True) not in response.text


def test_history_hides_the_edit_action_past_the_cutoff(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-006: ``editable`` is server-side, so the action disappears."""
    _freeze(monkeypatch, _AFTER_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)
    _seed_override(postgres_engine, rule_id=rule_id, gym_account_id=gym_account_id)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    # The day still renders as modified; only the actions are withheld.
    assert "wb-chip--modified" in response.text
    assert _override_url(rule_id) not in response.text
    assert _override_url(rule_id, revert=True) not in response.text


def test_history_not_validated_warning_tracks_the_validated_flag(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-005 (render half): the warning is shown only when unvalidated."""
    _freeze(monkeypatch, _BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)
    _seed_override(postgres_engine, rule_id=rule_id, gym_account_id=gym_account_id, validated=False)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        unvalidated = client.get("/history")
        with postgres_engine.begin() as conn:
            conn.execute(text("UPDATE booking_day_override SET validated = true"))
        validated = client.get("/history")

    assert "not validated against a published schedule" in unvalidated.text
    assert "not validated against a published schedule" not in validated.text


# ---------------------------------------------------------------------------
# Upcoming: the skipped state and its actions (T-BDO-011)
# ---------------------------------------------------------------------------


def test_history_renders_the_skipped_day_with_revert_and_no_cancel(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FR-019, FR-022: the skipped chip, its own edit and revert actions,
    and no cancel action, because there will be no booking to cancel."""
    _freeze(monkeypatch, _BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)
    _seed_override(postgres_engine, rule_id=rule_id, gym_account_id=gym_account_id, skip_day=True)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        response = client.get("/history")

    assert response.status_code == 200
    body = response.text
    assert "wb-chip--skipped-day" in body
    assert "will be skipped" in body
    assert _override_url(rule_id) in body
    assert _override_url(rule_id, revert=True) in body
    # The cancel form only ever renders on a granted upcoming row.
    assert "wb-upcoming__cancel" not in body
    # The skip is not a mistargeted override, so no validation warning.
    assert "not validated against a published schedule" not in body


def test_history_renders_the_skipped_chip_in_spanish(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """i18n parity: the new chip resolves in both catalogs, not only EN."""
    _freeze(monkeypatch, _BEFORE_CUTOFF)
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    rule_id, gym_account_id = _seed_rule(postgres_engine, op_id)
    _seed_override(postgres_engine, rule_id=rule_id, gym_account_id=gym_account_id, skip_day=True)

    app = app_factory()
    with _sign_in(app, subject, "Alice", monkeypatch) as client:
        english = client.get("/history")
        spanish = client.get("/es/history")

    assert "will be skipped" in english.text
    assert "se saltará" in spanish.text
    assert "chip.skipped_day" not in spanish.text
