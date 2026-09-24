"""Component tests for the statistics page (T-AST-007, T-AST-014).

Exercises the route end to end: session, gym scoping, capture on open,
metrics, and the markup contract that keeps the page honest when the
charting library is absent.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.security.cipher import Cipher
from wodbuster_worker.wodbuster_client.client import LoadClassResponse
from wodbuster_worker.wodbuster_client.parsers import operator_idu_to_guid

from .conftest import gym_account_id_for

OTHER_IDU = "11112222333344445555666677778888"


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
    assert tc.get("/auth/microsoft/callback?code=fake&state=fake").status_code == 302
    return tc


def _athlete(idu: str, *, fecha_estado: str = "20/09/2026 22:40:00") -> dict[str, Any]:
    guid = operator_idu_to_guid(idu)
    return {
        "Id": 94,
        "DisplayName": "Someone Else",
        "Url": f"/athlete/athletes.aspx?gid={guid}",
        "UrlFoto": f"https://cdn.wodbuster.com/static/atletas/a/a/e/{guid}.jpg",
        "FechaEstado": fecha_estado,
        "TipoReserva": "Tarifa",
    }


def _payload_for(idu: str) -> dict[str, Any]:
    valor = {
        "Id": 47459,
        "Nombre": "Cross Training",
        "IdTipoEntrenamiento": 1,
        "HoraComienzo": "20:30:00",
        "Plazas": 14,
        "AlgunMomentoLlena": True,
        "AtletasEntrenando": [_athlete(OTHER_IDU), _athlete(idu)],
        "AtletasBorradosVisibles": [],
        "AtletasNoEntrenandoVisibles": [],
    }
    return {"Data": [{"Hora": "20:30:00", "Valores": [{"Valor": valor}]}]}


class RecordingClient:
    """Returns one gym day, and refuses to mutate anything.

    ``include_operator`` is off by default so a test that asserts a
    count is not silently competing with fabricated attendance for every
    captured day.
    """

    def __init__(self, idu: str, *, include_operator: bool = False) -> None:
        self.idu = idu
        self.include_operator = include_operator
        self.calls = 0
        self.ticks: list[int] = []

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        self.calls += 1
        self.ticks.append(ticks)
        return LoadClassResponse(
            status_code=200,
            latency_ms=100.0,
            payload=_payload_for(self.idu if self.include_operator else OTHER_IDU),
        )

    def discover_idu(self, cookie_value: str) -> str:
        return self.idu

    def inscribir(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the statistics page must never book")

    def borrar(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("the statistics page must never cancel")


def _idu_of(engine: Engine, gym_account_id: int) -> str:
    with engine.connect() as conn:
        return str(
            conn.execute(
                text("SELECT idu FROM gym_account WHERE id = :id"), {"id": gym_account_id}
            ).scalar_one()
        )


def _seed_attendance(
    engine: Engine,
    *,
    gym_account_id: int,
    local_date: date,
    state: str = "attended",
    class_id: int = 90001,
    hour: int = 18,
    changed_at: datetime | None = None,
) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO attendance_day (gym_account_id, local_date, class_count, is_final) "
                "VALUES (:ga, :d, 20, TRUE) ON CONFLICT DO NOTHING"
            ),
            {"ga": gym_account_id, "d": local_date},
        )
        conn.execute(
            text(
                "INSERT INTO attendance_record "
                "(gym_account_id, local_date, wodbuster_class_id, class_name, "
                "start_at, state, state_changed_at, occupancy, capacity) "
                "VALUES (:ga, :d, :cid, 'Cross Training', :start, :state, :changed, 14, 14)"
            ),
            {
                "ga": gym_account_id,
                "d": local_date,
                "cid": class_id,
                "start": datetime(
                    local_date.year, local_date.month, local_date.day, hour, 30, tzinfo=UTC
                ),
                "changed": changed_at
                or datetime(
                    local_date.year, local_date.month, local_date.day, hour - 2, 0, tzinfo=UTC
                ),
                "state": state,
            },
        )


@pytest.fixture
def signed_in(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[TestClient, int, int, RecordingClient]:
    operator_id, subject = seed_operator(display_name="Statistician")
    with postgres_engine.begin() as conn:
        gym_account_id = gym_account_id_for(conn, operator_id)
    idu = _idu_of(postgres_engine, gym_account_id)

    app = app_factory()
    client = RecordingClient(idu)
    app.state.wodbuster_client = client
    app.state.booking_client_factory = None

    # Capture needs a cookie on file; without one the route skips it and
    # renders from stored history, which is a different path. The app
    # factory leaves the cookie stack unwired because the test secrets
    # carry no encryption key, so install a real one here.
    cipher = Cipher(os.urandom(32))
    store = CookieStore(cipher)
    app.state.cipher = cipher
    app.state.cookie_store = store
    with sessionmaker(bind=postgres_engine)() as session:
        store.save(session, gym_account_id, ".WBAuth-tok", validated_at=datetime.now(tz=UTC))
        session.commit()

    tc = _sign_in(app, subject, "Statistician", monkeypatch)
    return tc, operator_id, gym_account_id, client


def test_page_renders_with_the_navigation_entry(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    tc, _, _, _ = signed_in

    response = tc.get("/statistics")

    assert response.status_code == 200
    body = response.text
    assert "Statistics" in body
    assert 'href="/statistics"' in body
    assert 'aria-current="page"' in body


def test_page_counts_a_settled_attendance(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """CC-001: an attended class shows up in the headline count."""
    tc, _, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(postgres_engine, gym_account_id=gym_account_id, local_date=yesterday)

    body = tc.get("/statistics").text

    assert 'class="wb-stat-tile__value">1<' in body


def test_every_number_is_present_without_any_script(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """FR-034: the calendar is a table, so it is its own text equivalent."""
    tc, _, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(postgres_engine, gym_account_id=gym_account_id, local_date=yesterday)

    body = tc.get("/statistics").text

    assert "wb-calendar" in body
    assert f'<time datetime="{yesterday.isoformat()}">' in body
    # No canvas, so nothing can disappear when a script fails to load.
    assert "<canvas" not in body


def test_a_dropped_day_is_visible_on_the_page(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """A day booked and then dropped must not look like an idle day."""
    tc, _, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=yesterday,
        state="cancelled",
    )

    body = tc.get("/statistics").text

    assert "wb-calendar__day--cancelled" in body


def test_no_charting_library_is_loaded_yet(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """The page shows a calendar, which is a table and not a chart.

    Chart.js stays pinned in ADR-0014 and arrives with the first metric
    that needs a canvas. Loading 219 KB before then would be paid on
    every visit for nothing.
    """
    body = signed_in[0].get("/statistics").text

    assert "chart.js" not in body
    assert "chartjs-plugin-zoom" not in body


def test_the_month_can_be_walked_backwards(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """The current month offers a way back and no way forward."""
    tc, _, _, _ = signed_in
    today = datetime.now(tz=UTC).date()
    previous = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    body = tc.get("/statistics").text

    assert f"?month={previous}" in body
    assert f"?month={today.strftime('%Y-%m')}" not in body

    older = tc.get(f"/statistics?month={previous}")
    assert older.status_code == 200
    # From a past month, both arrows exist.
    assert older.text.count("?month=") == 2


def test_a_crafted_month_parameter_does_not_break_the_page(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    tc, _, _, _ = signed_in

    for value in ("nonsense", "2026-13", "9999-99", ""):
        assert tc.get(f"/statistics?month={value}").status_code == 200


def test_a_class_change_is_painted_on_its_own_day(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """The whole cell turns violet, so the change is findable at a glance."""
    tc, _, gym_account_id, _ = signed_in
    day = datetime.now(tz=UTC).date() - timedelta(days=1)
    moved_at = datetime(day.year, day.month, day.day, 14, 0, tzinfo=UTC)
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=day,
        state="cancelled",
        class_id=90101,
        hour=16,
        changed_at=moved_at,
    )
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=day,
        state="attended",
        class_id=90102,
        hour=17,
        changed_at=moved_at,
    )

    body = tc.get("/statistics").text

    assert "wb-calendar__day--swapped" in body
    # The state text is gone: the colour carries it, and the cell keeps
    # the class name instead.
    assert "wb-calendar__swap" not in body


def test_the_month_can_be_reached_with_a_date_picker(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """A plain GET form, so it works with the picker, with a typed
    value, and with no JavaScript at all."""
    tc, _, _, _ = signed_in

    body = tc.get("/statistics").text

    assert 'class="wb-monthjump"' in body
    assert 'method="get"' in body
    assert "wb-date-flatpickr" in body

    # The picker posts a whole day; the month is what matters.
    jumped = tc.get("/statistics?month=2026-07-14")
    assert jumped.status_code == 200
    assert "?month=2026-06" in jumped.text


def test_no_third_party_athlete_reaches_the_page(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """INV-001 at the response boundary.

    Asserted on the other athlete's own identifiers rather than on the
    CDN host: the operator's own profile photo legitimately comes from
    that host, so a blanket host check would forbid something correct.
    """
    body = signed_in[0].get("/statistics").text
    other_guid = operator_idu_to_guid(OTHER_IDU)

    assert "Someone Else" not in body
    assert other_guid not in body
    assert "athletes.aspx" not in body


def test_opening_the_page_captures_and_never_books(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """The page is the capture trigger (FR-010) and only ever reads."""
    tc, _, gym_account_id, client = signed_in

    tc.get("/statistics")

    assert client.calls > 0
    with postgres_engine.connect() as conn:
        days = conn.execute(
            text("SELECT COUNT(*) FROM attendance_day WHERE gym_account_id = :ga"),
            {"ga": gym_account_id},
        ).scalar_one()
    assert days > 0


def test_a_second_visit_does_not_re_read_finished_days(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """CC-007: a finished day is read once, ever.

    A visit is capped, so a second one continues backwards instead of
    repeating itself. The only day both visits touch is today, whose
    ledger row stays provisional until the day ends.
    """
    tc, _, _, client = signed_in

    tc.get("/statistics")
    first_pass = set(client.ticks)
    client.ticks.clear()
    tc.get("/statistics")
    second_pass = set(client.ticks)

    assert len(first_pass) > 1
    assert len(second_pass) > 1
    assert len(first_pass & second_pass) == 1, "only today may be read twice"


def test_another_users_history_is_unreachable(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-021 and CC-022: figures derive from the acting user's account."""
    alice_id, alice_subject = seed_operator(display_name="Alice")
    bob_id, _ = seed_operator(display_name="Bob")
    with postgres_engine.begin() as conn:
        bob_gym = gym_account_id_for(conn, bob_id)
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(postgres_engine, gym_account_id=bob_gym, local_date=yesterday)

    app = app_factory()
    app.state.wodbuster_client = None
    app.state.booking_client_factory = None
    tc = _sign_in(app, alice_subject, "Alice", monkeypatch)

    body = tc.get("/statistics").text

    # Alice has never had a day captured, so she gets the empty state,
    # and Bob's single attendance appears nowhere in her page.
    assert "Nothing has been read from your gym calendar yet" in body
    assert 'class="wb-stat-tile__value">1<' not in body
    # A crafted parameter changes nothing: the account comes from the
    # session, and the route never reads one from the query string.
    assert tc.get(f"/statistics?gym_account_id={bob_gym}").text == body
    assert alice_id != bob_id


def test_page_renders_without_a_client_stack(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-012: no cookie or client means stale data, never a 500."""
    _, subject = seed_operator(display_name="Cookieless")
    app = app_factory()
    app.state.wodbuster_client = None
    app.state.booking_client_factory = None
    tc = _sign_in(app, subject, "Cookieless", monkeypatch)

    response = tc.get("/statistics")

    assert response.status_code == 200
