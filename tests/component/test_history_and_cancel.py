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
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from wodbuster_worker.persistence.models import BookingOutcome, NotificationOutbox
from wodbuster_worker.wodbuster_client.client import BookingActionResponse, LoadClassResponse


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
    columns = "(operator_id, target_class, target_slot, terminal_status"
    values = "(:op, :cls, :slot, :status"
    params: dict[str, Any] = {
        "op": operator_id,
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
        conn.execute(
            text(
                "INSERT INTO scheduler_rule ("
                " operator_id, day_of_week, class_type, class_time, "
                " booking_opens_days_before, booking_opens_at, active"
                ") VALUES (:op, :dow, 'ScheduledWOD', '21:30', 0, '21:30', true)"
            ),
            {"op": op_id, "dow": attend_dow},
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
    op_id, subject = seed_operator(provider="microsoft", display_name="Alice")
    booking_id = _seed_booking(
        postgres_engine,
        operator_id=op_id,
        target_class="WOD",
        target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
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
        outbox = session.query(NotificationOutbox).filter_by(operator_id=op_id).all()
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
