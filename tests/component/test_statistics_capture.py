"""Component tests for the attendance capture service (T-AST-004).

Runs against a real Postgres schema so the upsert behaviour that makes
capture idempotent is exercised against the real constraints, not
against an in-memory approximation.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from time import sleep
from typing import Any

import pytest
import structlog
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.statistics.capture import (
    capture_day,
    catch_up,
    fetch_day,
    ticks_for_local_date,
)
from wodbuster_worker.wodbuster_client.client import LoadClassResponse
from wodbuster_worker.wodbuster_client.parsers import operator_idu_to_guid

OPERATOR_IDU = "aae9b1c2d3e4f5061728394a5b6c7605"
OTHER_IDU = "11112222333344445555666677778888"
LOCAL_DATE = date(2026, 9, 23)


class _UpstreamDown(Exception):
    """Stands in for any client failure: transport, auth or protocol."""


class StubClient:
    """A client that can only read.

    ``inscribir`` and ``borrar`` raise on sight: capture must never
    reach them (INV-007), and a stub that merely lacks them would let a
    typo fail as an AttributeError somewhere far from the cause.
    """

    def __init__(
        self,
        payload: dict[str, Any] | None = None,
        *,
        fail: bool = False,
        delay_seconds: float = 0.0,
    ) -> None:
        self.payload = payload if payload is not None else _payload()
        self.fail = fail
        self.delay_seconds = delay_seconds
        self.calls: list[int] = []

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        self.calls.append(ticks)
        if self.fail:
            raise _UpstreamDown("upstream refused")
        if self.delay_seconds:
            sleep(self.delay_seconds)
        return LoadClassResponse(status_code=200, latency_ms=120.0, payload=self.payload)

    def inscribir(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("capture must never book")

    def borrar(self, *args: Any, **kwargs: Any) -> None:
        raise AssertionError("capture must never cancel")


def _athlete(idu: str, *, fecha_estado: str | None = "20/09/2026 22:40:00") -> dict[str, Any]:
    guid = operator_idu_to_guid(idu)
    entry: dict[str, Any] = {
        "Id": 94,
        "DisplayName": "Someone Else",
        "Url": f"/athlete/athletes.aspx?gid={guid}",
        "UrlFoto": f"https://cdn.wodbuster.com/static/atletas/a/a/e/{guid}.jpg",
        "TipoReserva": "Tarifa",
    }
    if fecha_estado is not None:
        entry["FechaEstado"] = fecha_estado
    return entry


def _valor(
    *,
    class_id: int = 47459,
    hora: str = "20:30:00",
    attending: list[dict[str, Any]] | None = None,
    removed: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "Id": class_id,
        "Nombre": "Cross Training",
        "IdTipoEntrenamiento": 1,
        "HoraComienzo": hora,
        "Plazas": 14,
        "AlgunMomentoLlena": True,
        "AtletasEntrenando": attending if attending is not None else [],
        "AtletasBorradosVisibles": removed if removed is not None else [],
        "AtletasNoEntrenandoVisibles": [],
    }


def _payload(*valores: dict[str, Any]) -> dict[str, Any]:
    chosen = valores or (
        _valor(attending=[_athlete(OTHER_IDU) for _ in range(13)] + [_athlete(OPERATOR_IDU)]),
        _valor(class_id=47460, hora="07:00:00", attending=[_athlete(OTHER_IDU)]),
    )
    return {"Data": [{"Hora": v["HoraComienzo"], "Valores": [{"Valor": v}]} for v in chosen]}


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(bind=postgres_engine, autoflush=False, expire_on_commit=False)


@pytest.fixture
def gym_account_id(postgres_engine: Engine) -> int:
    with postgres_engine.begin() as conn:
        op_id = conn.execute(
            text("INSERT INTO operator_profile (display_name) VALUES ('Captured') RETURNING id")
        ).scalar_one()
        return int(
            conn.execute(
                text(
                    "INSERT INTO gym_account (user_id, gym_slug, display_name, idu) "
                    "VALUES (:op, 'antworktrainingcenter', 'Antwork', :idu) RETURNING id"
                ),
                {"op": op_id, "idu": OPERATOR_IDU},
            ).scalar_one()
        )


def _rows(engine: Engine, statement: str) -> list[Any]:
    with engine.connect() as conn:
        return list(conn.execute(text(statement)).all())


def _capture(
    session_factory: sessionmaker[Session],
    gym_account_id: int,
    client: StubClient,
    *,
    local_date: date = LOCAL_DATE,
    now: datetime | None = None,
) -> Any:
    with session_factory() as session, session.begin():
        return capture_day(
            session,
            gym_account_id=gym_account_id,
            local_date=local_date,
            client=client,
            cookie_value="cookie",
            operator_idu=OPERATOR_IDU,
            now=now if now is not None else datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
        )


def test_capture_writes_the_operator_row_and_the_ledger(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    gym_account_id: int,
) -> None:
    """CC-001: an attended class is stored and the day is recorded."""
    client = StubClient()

    result = _capture(session_factory, gym_account_id, client)

    assert result.class_count == 2
    assert result.records_written == 1
    assert result.is_final is True

    records = _rows(
        postgres_engine,
        "SELECT wodbuster_class_id, state, occupancy, capacity, ever_full, "
        "class_type_id, class_name FROM attendance_record",
    )
    assert len(records) == 1
    row = records[0]
    assert row.wodbuster_class_id == 47459
    assert row.state == "attended"
    assert row.occupancy == 14
    assert row.capacity == 14
    assert row.ever_full is True
    assert row.class_type_id == 1
    assert row.class_name == "Cross Training"

    days = _rows(postgres_engine, "SELECT local_date, class_count, is_final FROM attendance_day")
    assert len(days) == 1
    assert days[0].local_date == LOCAL_DATE
    assert days[0].class_count == 2
    assert days[0].is_final is True


def test_capture_stores_a_cancellation_with_its_instant(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    gym_account_id: int,
) -> None:
    """CC-002 and CC-004: the observed 21/09 case, removed at 18:29."""
    client = StubClient(
        _payload(
            _valor(
                class_id=47396,
                attending=[_athlete(OTHER_IDU) for _ in range(14)],
                removed=[_athlete(OPERATOR_IDU, fecha_estado="21/09/2026 18:29:00")],
            )
        )
    )

    _capture(session_factory, gym_account_id, client, local_date=date(2026, 9, 21))

    rows = _rows(postgres_engine, "SELECT state, start_at, state_changed_at FROM attendance_record")
    assert len(rows) == 1
    assert rows[0].state == "cancelled"
    # 20:30 Madrid in September is 18:30 UTC; 18:29 local is 16:29 UTC.
    assert rows[0].start_at == datetime(2026, 9, 21, 18, 30, tzinfo=UTC)
    assert rows[0].state_changed_at == datetime(2026, 9, 21, 16, 29, tzinfo=UTC)


def test_capture_never_books_or_cancels(
    session_factory: sessionmaker[Session],
    gym_account_id: int,
) -> None:
    """INV-007: the client's mutating methods raise if capture touches them."""
    client = StubClient()

    _capture(session_factory, gym_account_id, client)

    assert client.calls == [ticks_for_local_date(LOCAL_DATE)]


def test_a_day_with_classes_and_no_activity_is_still_recorded(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    gym_account_id: int,
) -> None:
    """INV-005: the ledger separates "not looked" from "did not train"."""
    client = StubClient(_payload(_valor(attending=[_athlete(OTHER_IDU)])))

    result = _capture(session_factory, gym_account_id, client)

    assert result.records_written == 0
    assert result.class_count == 1
    days = _rows(postgres_engine, "SELECT class_count FROM attendance_day")
    assert days[0].class_count == 1
    assert _rows(postgres_engine, "SELECT id FROM attendance_record") == []


def test_capturing_the_same_day_twice_is_idempotent(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    gym_account_id: int,
) -> None:
    """CC-007: repeated capture produces the same rows, never duplicates.

    Skipping the second upstream call for a final day is the catch-up
    layer's job (T-AST-008); what is asserted here is that doing it
    twice cannot corrupt anything.
    """
    client = StubClient()

    _capture(session_factory, gym_account_id, client)
    _capture(session_factory, gym_account_id, client)

    assert len(_rows(postgres_engine, "SELECT id FROM attendance_record")) == 1
    assert len(_rows(postgres_engine, "SELECT id FROM attendance_day")) == 1


def test_concurrent_capture_of_the_same_day_does_not_duplicate(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    gym_account_id: int,
) -> None:
    """Two page loads at once: the constraint absorbs the second write."""
    first = session_factory()
    second = session_factory()
    try:
        with first.begin():
            capture_day(
                first,
                gym_account_id=gym_account_id,
                local_date=LOCAL_DATE,
                client=StubClient(),
                cookie_value="cookie",
                operator_idu=OPERATOR_IDU,
                now=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
            )
        with second.begin():
            capture_day(
                second,
                gym_account_id=gym_account_id,
                local_date=LOCAL_DATE,
                client=StubClient(),
                cookie_value="cookie",
                operator_idu=OPERATOR_IDU,
                now=datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
            )
    finally:
        first.close()
        second.close()

    assert len(_rows(postgres_engine, "SELECT id FROM attendance_record")) == 1


def test_today_is_recorded_as_provisional(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    gym_account_id: int,
) -> None:
    """CC-008: a day still running is never final."""
    client = StubClient()

    result = _capture(
        session_factory,
        gym_account_id,
        client,
        now=datetime(2026, 9, 23, 21, 0, tzinfo=UTC),
    )

    assert result.is_final is False
    assert _rows(postgres_engine, "SELECT is_final FROM attendance_day")[0].is_final is False


def test_an_upstream_failure_records_nothing(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
    gym_account_id: int,
) -> None:
    """INV-009 and CC-012: a day we could not read stays absent."""
    client = StubClient(fail=True)

    with pytest.raises(_UpstreamDown), session_factory() as session, session.begin():
        capture_day(
            session,
            gym_account_id=gym_account_id,
            local_date=LOCAL_DATE,
            client=client,
            cookie_value="cookie",
            operator_idu=OPERATOR_IDU,
        )

    assert _rows(postgres_engine, "SELECT id FROM attendance_day") == []
    assert _rows(postgres_engine, "SELECT id FROM attendance_record") == []


def test_capture_logs_nothing_taken_from_an_athlete_entry(
    session_factory: sessionmaker[Session],
    gym_account_id: int,
) -> None:
    """INV-001 at the logging boundary, which is the easiest place to leak."""
    with structlog.testing.capture_logs() as logs:
        _capture(session_factory, gym_account_id, StubClient())

    rendered = repr(logs)
    assert logs, "expected the capture path to log something"
    assert "Someone Else" not in rendered
    assert "cdn.wodbuster.com" not in rendered
    assert "athletes.aspx" not in rendered


def test_fetch_day_touches_no_database(gym_account_id: int) -> None:
    """The network half is separable, which is what keeps a failed
    upstream call from ever opening a transaction."""
    capture = fetch_day(
        StubClient(),
        cookie_value="cookie",
        local_date=LOCAL_DATE,
        operator_idu=OPERATOR_IDU,
    )

    assert capture.local_date == LOCAL_DATE
    assert capture.class_count == 2
    assert [s.state for s in capture.states] == ["attended"]


def _catch_up(
    gym_account_id: int,
    client: StubClient,
    *,
    cap: int = 5,
    priority: tuple[date, date] | None = None,
    budget_seconds: float | None = None,
    now: datetime | None = None,
) -> Any:
    return catch_up(
        gym_account_id=gym_account_id,
        client=client,
        cookie_value="cookie",
        operator_idu=OPERATOR_IDU,
        cap=cap,
        horizon_days=365,
        priority=priority,
        budget_seconds=budget_seconds,
        now=now if now is not None else datetime(2026, 9, 24, 10, 0, tzinfo=UTC),
    )


def test_catch_up_reads_the_most_recent_days_first(gym_account_id: int) -> None:
    client = StubClient()

    result = _catch_up(gym_account_id, client, cap=3)

    assert result.captured == (date(2026, 9, 24), date(2026, 9, 23), date(2026, 9, 22))


def test_catch_up_reads_the_month_on_screen_first(gym_account_id: int) -> None:
    """Opening January must read January.

    Without this, the budget goes to the days nearest today and the
    month the reader is looking at stays blank however many times they
    open it.
    """
    client = StubClient()
    january = (date(2026, 1, 1), date(2026, 1, 31))

    result = _catch_up(gym_account_id, client, cap=3, priority=january)

    assert result.captured == (date(2026, 1, 31), date(2026, 1, 30), date(2026, 1, 29))


def test_catch_up_continues_outside_the_priority_once_it_is_covered(
    gym_account_id: int,
) -> None:
    """A fully read month does not stall the general backfill."""
    client = StubClient()
    one_day = (date(2026, 9, 20), date(2026, 9, 20))

    result = _catch_up(gym_account_id, client, cap=3, priority=one_day)

    assert result.captured[0] == date(2026, 9, 20)
    assert len(result.captured) == 3
    assert result.captured[1] == date(2026, 9, 24)


def test_catch_up_stops_when_the_time_budget_runs_out(gym_account_id: int) -> None:
    """The count alone is not a bound: it assumes every call is fast."""
    client = StubClient(delay_seconds=0.05)

    result = _catch_up(gym_account_id, client, cap=30, budget_seconds=0.12)

    assert 1 <= len(result.captured) < 30
    assert result.stopped_early is True


def test_catch_up_reports_work_left_to_do(gym_account_id: int) -> None:
    """The page uses this to say "come back" rather than "nothing here"."""
    client = StubClient()

    result = _catch_up(gym_account_id, client, cap=2)

    assert result.remaining > 0
    assert result.stopped_early is True
