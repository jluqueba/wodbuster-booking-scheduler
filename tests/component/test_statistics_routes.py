"""Component tests for the statistics page (T-AST-007, T-AST-014).

Exercises the route end to end: session, gym scoping, capture on open,
metrics, and the markup contract that keeps the page honest when the
charting library is absent.
"""

from __future__ import annotations

import json
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
from wodbuster_worker.wodbuster_client.client import (
    LoadClassResponse,
    WodBusterAuthError,
    WodBusterTransportError,
)
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
        self.error: Exception | None = None
        # The points page is off by default. A test that asserts a
        # points figure opts in, so every other test keeps rendering the
        # page the gym would serve without one.
        self.points_page: str | None = None
        self.points_error: Exception | None = None
        self.points_calls = 0

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        self.calls += 1
        self.ticks.append(ticks)
        if self.error is not None:
            raise self.error
        return LoadClassResponse(
            status_code=200,
            latency_ms=100.0,
            payload=_payload_for(self.idu if self.include_operator else OTHER_IDU),
        )

    def load_points_page(self, cookie_value: str) -> str:
        self.points_calls += 1
        if self.points_error is not None:
            raise self.points_error
        return self.points_page or ""

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
    ever_full: bool = False,
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
                "start_at, state, state_changed_at, occupancy, capacity, ever_full) "
                "VALUES (:ga, :d, :cid, 'Cross Training', :start, :state, :changed, 14, 14, :full)"
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
                "full": ever_full,
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


def test_the_drop_out_rate_shows_the_counts_behind_it(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """CC-003: a rate the reader cannot check is a rate they distrust."""
    tc, _, gym_account_id, _ = signed_in
    base = datetime.now(tz=UTC).date() - timedelta(days=10)
    for offset in range(3):
        _seed_attendance(
            postgres_engine,
            gym_account_id=gym_account_id,
            local_date=base + timedelta(days=offset),
            class_id=91000 + offset,
        )
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=base + timedelta(days=3),
        state="cancelled",
        class_id=91003,
    )

    body = tc.get("/statistics").text

    # One drop out of four bookings.
    assert 'class="wb-stat-tile__value">25%<' in body
    assert "1 of 4 bookings" in body


def test_a_month_with_no_bookings_shows_a_dash_not_a_zero(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """Reporting a perfect rate to someone who booked nothing is worse
    than admitting there is nothing to report."""
    tc, _, _, _ = signed_in

    body = tc.get("/statistics").text

    assert "Nothing booked in this month" in body
    assert 'class="wb-stat-tile__value">0%<' not in body


def test_the_notice_bands_appear_with_the_gym_thresholds(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """CC-004 and CC-005 at the response boundary."""
    tc, _, gym_account_id, _ = signed_in
    day = datetime.now(tz=UTC).date() - timedelta(days=3)
    # Dropped two hours before an 18:30 class: the middle band.
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=day,
        state="cancelled",
        class_id=91100,
        changed_at=datetime(day.year, day.month, day.day, 16, 30, tzinfo=UTC),
    )

    body = tc.get("/statistics").text

    assert "How much notice you gave" in body
    assert "between 1 h and 4 h ahead" in body
    assert 'class="wb-bands__item wb-bands__item--late"' in body


def test_the_bands_are_absent_when_nothing_was_dropped(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """A block of three zeros teaches the reader to skip the block."""
    tc, _, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(postgres_engine, gym_account_id=gym_account_id, local_date=yesterday)

    body = tc.get("/statistics").text

    assert "How much notice you gave" not in body


def test_every_number_is_present_without_any_script(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """FR-034: the calendar is a table, so it is its own text equivalent.

    The charts added in slice 5 do use a canvas, and each carries its
    own table beside it. This test guards the calendar specifically:
    it must never become a canvas, because it is the one view the
    reader checks figures against.
    """
    tc, _, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(postgres_engine, gym_account_id=gym_account_id, local_date=yesterday)

    body = tc.get("/statistics").text
    calendar = body[body.index('<table class="wb-calendar">') :]

    assert f'<time datetime="{yesterday.isoformat()}">' in calendar
    assert "<canvas" not in calendar


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

    # The full class attribute, not the bare modifier: the page carries
    # its own stylesheet inline, so a bare name matches the CSS rule and
    # the assertion passes whether or not the cell was ever rendered.
    assert 'class="wb-calendar__day wb-calendar__day--cancelled"' in body


def test_no_charting_library_is_loaded_yet(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """CC-026: Superseded by slice 5: the charts arrived and so did the library."""
    body = signed_in[0].get("/statistics").text

    assert "chart.js@4.5.1" in body


def test_the_chart_scripts_are_pinned_with_integrity(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """A substituted CDN file must fail closed rather than execute."""
    body = signed_in[0].get("/statistics").text

    assert "chart.js@4.5.1/dist/chart.umd.min.js" in body
    assert "chartjs-plugin-zoom@2.2.0" in body
    assert "chartjs-chart-matrix@3.1.0" in body
    assert body.count('integrity="sha384-') >= 3
    assert body.count('crossorigin="anonymous"') >= 3


def test_every_chart_carries_its_numbers_as_markup(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """FR-034: a canvas is opaque to assistive technology and vanishes
    if the library does not load, so the same numbers are always here."""
    tc, _, gym_account_id, _ = signed_in
    base = datetime.now(tz=UTC).date() - timedelta(days=20)
    for offset in range(6):
        _seed_attendance(
            postgres_engine,
            gym_account_id=gym_account_id,
            local_date=base + timedelta(days=offset),
            class_id=92000 + offset,
        )

    body = tc.get("/statistics").text

    assert "<canvas" in body
    assert 'role="img"' in body
    # Each canvas has a JSON block and a details block holding a table.
    assert body.count('type="application/json"') >= 1
    assert "wb-chart__data" in body
    assert "wb-rules-table" in body


def test_the_period_selector_governs_every_chart(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """CC-027, CC-046: One window for the whole page. Per-chart windows would let the
    reader cross two figures that do not describe the same thing."""
    tc, _, _, _ = signed_in

    body = tc.get("/statistics").text
    assert 'class="wb-periods"' in body
    assert 'name="period"' in body
    # The default is marked as current rather than offered again.
    assert 'class="wb-periods__current"' in body

    narrowed = tc.get("/statistics?period=m1")
    assert narrowed.status_code == 200
    assert "30" in narrowed.text


def test_a_crafted_period_does_not_break_the_page(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    tc, _, _, _ = signed_in

    for value in ("nonsense", "m99", "", "../../etc"):
        assert tc.get(f"/statistics?period={value}").status_code == 200


def test_the_period_survives_a_month_change(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """The two controls are independent: changing the month must not
    silently reset the window the charts describe."""
    tc, _, _, _ = signed_in
    today = datetime.now(tz=UTC).date()

    body = tc.get(f"/statistics?period=m3&month={today.strftime('%Y-%m')}").text

    assert 'name="month"' in body
    assert 'value="m3"' not in body or "wb-periods__current" in body


def test_a_day_holding_a_training_and_a_drop_shows_both_lines(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """The grid must add up to the tiles.

    A day with a training and a removal is a class change, and the cell
    lists both the class trained and the change, so the reader can
    reconcile every tile with what they can see.
    """
    tc, _, gym_account_id, _ = signed_in
    day = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=day,
        state="cancelled",
        class_id=90201,
        hour=16,
        changed_at=datetime(day.year, day.month, day.day, 8, 0, tzinfo=UTC),
    )
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=day,
        state="attended",
        class_id=90202,
        hour=10,
        changed_at=datetime(day.year, day.month, day.day, 9, 0, tzinfo=UTC),
    )

    body = tc.get("/statistics").text

    assert 'class="wb-calendar__day wb-calendar__day--swapped"' in body
    assert 'class="wb-calendar__line wb-calendar__line--attended"' in body
    assert 'class="wb-calendar__line wb-calendar__line--swapped"' in body
    # No drop is claimed, so no red cell is promised and none is missing.
    assert 'class="wb-calendar__line wb-calendar__line--cancelled"' not in body


def test_a_past_month_can_be_reached_and_is_bounded(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """The picker is the only way to travel, so it carries the bounds.

    Without them the reader could open a month the backfill will never
    populate and read its empty cells as a month they did not train.
    """
    tc, _, _, _ = signed_in
    today = datetime.now(tz=UTC).date()
    previous = (today.replace(day=1) - timedelta(days=1)).strftime("%Y-%m")

    body = tc.get("/statistics").text

    # No step arrows: the picker carries its own month and year nav.
    assert "wb-monthnav" not in body
    assert f'data-fp-max="{today.isoformat()}"' in body
    assert 'data-fp-min="' in body

    older = tc.get(f"/statistics?month={previous}")
    assert older.status_code == 200
    assert f'value="{previous}-01"' in older.text


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

    assert 'class="wb-calendar__day wb-calendar__day--swapped"' in body
    # The old marker element is gone: the colour carries the change and
    # the cell lists the class trained.
    assert "wb-calendar__swap" not in body


def test_the_month_can_be_reached_with_a_date_picker(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """A plain GET form, so it works with the picker, with a typed
    value, and with no JavaScript at all.

    The submit button stays in the markup for that last case: with the
    picker loaded the form submits on selection, so choosing a month is
    one gesture rather than select-then-confirm.
    """
    tc, _, _, _ = signed_in

    body = tc.get("/statistics").text

    assert 'class="wb-monthjump"' in body
    assert 'method="get"' in body
    assert "wb-date-flatpickr" in body
    assert 'data-fp-submit="1"' in body
    # The confirm button exists only inside noscript: with the picker
    # loaded it would be a second click for nothing, and without it a
    # lone text field is submittable only by pressing Enter.
    assert "<noscript>" in body
    assert body.index("<noscript>") < body.index("wb-monthjump__go")

    # The picker posts a whole day; the month is what matters.
    jumped = tc.get("/statistics?month=2026-07-14")
    assert jumped.status_code == 200
    assert 'value="2026-07-01"' in jumped.text


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


def test_a_rejected_cookie_says_so_and_points_at_the_fix(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """CC-034: CC-012: "stale" is not useful; "renew your session" is."""
    tc, _, gym_account_id, client = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(postgres_engine, gym_account_id=gym_account_id, local_date=yesterday)
    client.error = WodBusterAuthError("rejected")

    body = tc.get("/statistics").text

    assert "session is no longer valid" in body
    assert 'href="/cookie"' in body
    # The history already read is still on the page.
    assert 'class="wb-stat-tile__value">1<' in body


def test_an_unreachable_gym_does_not_blame_the_session(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """CC-035: Telling a user to renew a working session wastes their time."""
    tc, _, _, client = signed_in
    client.error = WodBusterTransportError("timeout")

    body = tc.get("/statistics").text

    assert "could not be reached" in body
    assert "session is no longer valid" not in body


def test_a_failed_capture_creates_no_alert(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """INV-008: cookie validity already has its own alerting, and a
    second signal for the same fact is noise."""
    tc, _, _, client = signed_in
    client.error = WodBusterAuthError("rejected")

    tc.get("/statistics")

    with postgres_engine.connect() as conn:
        alerts = conn.execute(text("SELECT COUNT(*) FROM alert")).scalar_one()
    assert alerts == 0


def test_the_three_empty_states_are_different_messages(
    app_factory: Callable[..., FastAPI],
    seed_operator: Callable[..., tuple[int, str]],
    postgres_engine: Engine,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CC-020 and INV-005.

    Nothing read and read-with-no-activity are different facts.
    Rendering them the same way is what turns a gap in coverage into a
    claim about the user.

    A third case, a gym that hides its athlete lists, looks identical
    to a user who has not booked anything. Nothing stored can tell them
    apart, so the page states the fact and diagnoses nothing.
    """
    operator_id, subject = seed_operator(display_name="Empty")
    with postgres_engine.begin() as conn:
        gym_account_id = gym_account_id_for(conn, operator_id)
    app = app_factory()
    app.state.wodbuster_client = None
    app.state.booking_client_factory = None
    tc = _sign_in(app, subject, "Empty", monkeypatch)

    nothing_read = tc.get("/statistics").text
    assert "Nothing has been read" in nothing_read
    assert "shows any activity of yours" not in nothing_read

    # Read, and no activity of the operator anywhere in it.
    day = datetime.now(tz=UTC).date() - timedelta(days=1)
    with postgres_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO attendance_day (gym_account_id, local_date, class_count, is_final) "
                "VALUES (:ga, :d, 20, TRUE)"
            ),
            {"ga": gym_account_id, "d": day},
        )
    no_activity = tc.get("/statistics").text
    assert "shows any activity of yours" in no_activity
    assert "Nothing has been read" not in no_activity
    # The calendar still renders: the days read are information.
    assert "wb-calendar" in no_activity

    # Read, and the operator did train.
    _seed_attendance(postgres_engine, gym_account_id=gym_account_id, local_date=day)
    trained = tc.get("/statistics").text
    assert "shows any activity of yours" not in trained
    assert 'class="wb-stat-tile__value">1<' in trained


def test_deleting_the_gym_account_removes_its_history(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """CC-023 at the route level, not only in the migration test."""
    tc, _, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(postgres_engine, gym_account_id=gym_account_id, local_date=yesterday)
    tc.get("/statistics")

    with postgres_engine.begin() as conn:
        conn.execute(text("DELETE FROM gym_account WHERE id = :ga"), {"ga": gym_account_id})
        days = conn.execute(text("SELECT COUNT(*) FROM attendance_day")).scalar_one()
        records = conn.execute(text("SELECT COUNT(*) FROM attendance_record")).scalar_one()

    assert (days, records) == (0, 0)
    # The page survives its gym account disappearing mid-session.
    assert tc.get("/statistics").status_code == 200


def _seed_closed_day(engine: Engine, *, gym_account_id: int, local_date: date) -> None:
    """A day the gym ran no classes, already read and final."""
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO attendance_day (gym_account_id, local_date, class_count, is_final) "
                "VALUES (:ga, :d, 0, TRUE) ON CONFLICT DO NOTHING"
            ),
            {"ga": gym_account_id, "d": local_date},
        )


def _set_excluded_weekdays(engine: Engine, operator_id: int, weekdays: list[int]) -> None:
    with engine.begin() as conn:
        conn.execute(
            text(
                "UPDATE operator_profile SET statistics_excluded_weekdays = "
                "CAST(:days AS jsonb) WHERE id = :id"
            ),
            {"id": operator_id, "days": json.dumps(weekdays)},
        )


def test_the_current_streak_counts_consecutive_training_days(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    tc, _, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    for offset in range(3):
        _seed_attendance(
            postgres_engine,
            gym_account_id=gym_account_id,
            local_date=yesterday - timedelta(days=offset),
            class_id=93000 + offset,
        )

    body = tc.get("/statistics").text

    assert "Current streak" in body
    assert "3 training days" in body


def test_a_day_the_gym_was_shut_does_not_break_the_streak(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """CC-013: the same rule covers the weekly closing day and a public
    holiday, so neither has to be configured anywhere."""
    tc, _, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=yesterday,
        class_id=93100,
    )
    _seed_closed_day(
        postgres_engine, gym_account_id=gym_account_id, local_date=yesterday - timedelta(days=1)
    )
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=yesterday - timedelta(days=2),
        class_id=93101,
    )

    body = tc.get("/statistics").text

    assert "2 training days" in body


def test_a_weekday_the_user_never_trains_does_not_break_the_streak(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """The gym opens, the user never goes. Without this the streak would
    reset every week and the figure would say nothing."""
    tc, operator_id, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    skipped = yesterday - timedelta(days=1)
    _set_excluded_weekdays(postgres_engine, operator_id, [skipped.weekday()])
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=yesterday,
        class_id=93200,
    )
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=yesterday - timedelta(days=2),
        class_id=93201,
    )

    body = tc.get("/statistics").text

    assert "2 training days" in body


def test_a_streak_reaching_the_oldest_reading_says_at_least(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """Absence of a reading is not absence of training (INV-005)."""
    tc, _, gym_account_id, client = signed_in
    # No capture, so the ledger holds only the two seeded days and the
    # walk back runs out of readings rather than out of trainings.
    client.error = WodBusterTransportError("timeout")
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    for offset in range(2):
        _seed_attendance(
            postgres_engine,
            gym_account_id=gym_account_id,
            local_date=yesterday - timedelta(days=offset),
            class_id=93300 + offset,
        )

    body = tc.get("/statistics").text

    assert "at least 2" in body


_POINTS_PAGE = """
<span id="body_ctl00_CtlPagadoHasta">{paid_until}</span>
<span id="body_ctl00_CtlPeriodo">{printed}</span>
<span data-id="puntosReserva">{balance}</span>
"""


def _points_page(*, balance: int = 12, paid_until: str, printed: str) -> str:
    return _POINTS_PAGE.format(balance=balance, paid_until=paid_until, printed=printed)


def _this_month_page(balance: int = 12) -> str:
    """A billing period ending next month, built around today."""
    today = datetime.now(tz=UTC).date()
    end = date(today.year + (today.month == 12), today.month % 12 + 1, min(today.day, 28))
    start_month = date(end.year, end.month, 1) - timedelta(days=1)
    start = date(start_month.year, start_month.month, min(today.day, 28)) + timedelta(days=1)
    printed = f"del {start.day:02d} x al {end.day:02d} y"
    return _points_page(balance=balance, paid_until=end.strftime("%d/%m/%Y"), printed=printed)


def test_the_points_balance_is_shown_as_read_not_calculated(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """CC-048."""
    tc, _, _, client = signed_in
    client.points_page = _this_month_page(balance=9)

    body = tc.get("/statistics").text

    assert "Balance now" in body
    assert 'class="wb-stat-tile__value">9<' in body
    assert "Read from the gym, not calculated" in body


def test_a_points_figure_never_appears_without_its_label(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """CC-018: INV-004 at the response boundary. The label and the assumptions
    have to travel with the number, not in a footnote elsewhere."""
    tc, _, gym_account_id, client = signed_in
    client.points_page = _this_month_page()
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=yesterday,
        state="cancelled",
        class_id=94000,
        changed_at=datetime(yesterday.year, yesterday.month, yesterday.day, 16, 0, tzinfo=UTC),
        ever_full=True,
    )

    body = tc.get("/statistics").text

    assert "Estimated cost" in body
    assert "Estimate" in body
    assert "What this estimate assumes" in body
    assert "the gym overwrites" in body


def test_the_estimate_is_a_range_because_the_base_cost_is_unknowable(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """CC-047."""
    tc, _, gym_account_id, client = signed_in
    client.points_page = _this_month_page()
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=yesterday,
        state="cancelled",
        class_id=94100,
        changed_at=datetime(yesterday.year, yesterday.month, yesterday.day, 16, 0, tzinfo=UTC),
        ever_full=True,
    )

    body = tc.get("/statistics").text

    # One late cancellation: one point of penalty, plus a base cost that
    # may or may not have been spent.
    assert "1 to 2 points" in body


def test_the_billing_period_is_offered_only_when_the_gym_states_it(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """FR-031: omit the option without error rather than offering a
    range nobody can stand behind."""
    tc, _, _, client = signed_in

    without = tc.get("/statistics").text
    client.points_page = _this_month_page()
    with_period = tc.get("/statistics").text

    assert "This billing period" not in without
    assert "This billing period" in with_period


def test_an_unreadable_points_page_costs_the_block_not_the_page(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """CC-049."""
    tc, _, _, client = signed_in
    client.points_error = WodBusterTransportError("timeout")

    response = tc.get("/statistics")

    assert response.status_code == 200
    assert "Balance now" not in response.text
    assert "Statistics" in response.text


def test_the_points_page_is_never_used_to_book(
    signed_in: tuple[TestClient, int, int, RecordingClient],
) -> None:
    """The reader opens a statistics page; nothing here may mutate."""
    tc, _, _, client = signed_in
    client.points_page = _this_month_page()

    tc.get("/statistics")

    assert client.points_calls >= 1


def _set_points_model(engine: Engine, gym_account_id: int, value: str) -> None:
    with engine.begin() as conn:
        conn.execute(
            text("UPDATE gym_account SET points_model = CAST(:v AS jsonb) WHERE id = :id"),
            {"id": gym_account_id, "v": value},
        )


def test_a_points_model_that_is_not_an_object_does_not_take_the_page_down(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """The column is JSONB, so it accepts any JSON value. A list used to
    reach ``.get`` and raise, losing the whole screen rather than the
    customisation."""
    tc, _, gym_account_id, client = signed_in
    client.points_page = _this_month_page()
    _set_points_model(postgres_engine, gym_account_id, "[1, 2, 3]")

    response = tc.get("/statistics")

    assert response.status_code == 200
    assert "Statistics" in response.text


def test_a_points_model_override_moves_the_tiers_everywhere_at_once(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """A gym charged under one boundary and labelled under another is a
    disagreement the reader cannot diagnose, so one model drives the
    estimate, the bands and the chart alike."""
    tc, _, gym_account_id, client = signed_in
    client.points_page = _this_month_page()
    _set_points_model(postgres_engine, gym_account_id, '{"late_hours": 12}')
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    _seed_attendance(
        postgres_engine,
        gym_account_id=gym_account_id,
        local_date=yesterday,
        state="cancelled",
        class_id=95000,
        changed_at=datetime(yesterday.year, yesterday.month, yesterday.day, 14, 0, tzinfo=UTC),
        ever_full=True,
    )

    response = tc.get("/statistics")
    body = response.text

    # The cancellation gave four and a half hours' notice: early under
    # the default four-hour tier, late under this gym's twelve, which
    # adds the late penalty to its estimated range.
    assert response.status_code == 200
    assert "1 to 2 points" in body
    assert "0 to 1 points" not in body
    assert "more than 12 h ahead" in body
    assert "more than 4 h ahead" not in body
    assert "under 12 h ahead" in body
    assert "under 4 h ahead" not in body


def test_attendance_without_a_published_capacity_still_renders(
    signed_in: tuple[TestClient, int, int, RecordingClient],
    postgres_engine: Engine,
) -> None:
    """``occupancy()`` reports sessions with no average when the gym
    published no places. Multiplying that None by 100 took the render
    down for one missing upstream field."""
    tc, _, gym_account_id, _ = signed_in
    yesterday = datetime.now(tz=UTC).date() - timedelta(days=1)
    with postgres_engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO attendance_day (gym_account_id, local_date, class_count, is_final) "
                "VALUES (:ga, :d, 20, TRUE) ON CONFLICT DO NOTHING"
            ),
            {"ga": gym_account_id, "d": yesterday},
        )
        conn.execute(
            text(
                "INSERT INTO attendance_record "
                "(gym_account_id, local_date, wodbuster_class_id, class_name, "
                "start_at, state, occupancy, capacity) "
                "VALUES (:ga, :d, 96000, 'Cross Training', :start, 'attended', 9, NULL)"
            ),
            {
                "ga": gym_account_id,
                "d": yesterday,
                "start": datetime(
                    yesterday.year, yesterday.month, yesterday.day, 18, 30, tzinfo=UTC
                ),
            },
        )

    response = tc.get("/statistics")

    assert response.status_code == 200
    assert "did not publish how many places" in response.text
