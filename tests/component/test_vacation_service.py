"""Component tests for the vacation service (US7.1, US7.2, US7.T1-T3).

Covers three scenarios end-to-end against real Postgres:

- US7.T1: bulk cancel — three granted bookings on three different
  days, vacation covers the first two → those two get cancelled,
  the third stays granted.
- US7.T2: skip-guard boundary conditions — inclusive start,
  inclusive end, exclusive outside, ``closed_at`` short-circuits.
- US7.T3: auto-resume — a slot on the day after ``end_date`` is not
  covered so the scheduler proceeds normally.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.booking import vacation as vacation_service
from wodbuster_worker.booking.vacation import find_covering_window
from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.persistence.models import BookingOutcome, VacationWindow
from wodbuster_worker.security.cipher import Cipher
from wodbuster_worker.wodbuster_client.client import BookingActionResponse


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _StubClient:
    """Minimal CancelClientProtocol double: accepts every borrar as
    a granted cancel, and returns a synthetic LoadClass payload
    matching the seeded booking's class type + time."""

    slot_id: int = 999
    class_type: str = "WOD"
    class_time: str = "21:30"
    borrar_calls: list[dict[str, Any]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        self.borrar_calls = []

    def load_class(self, cookie_value: str, ticks: int) -> Any:
        return _StubLoadClass(
            slot_id=self.slot_id,
            class_type=self.class_type,
            class_time=self.class_time,
        )

    def borrar(
        self, cookie_value: str, *, class_id: int, ticks: int
    ) -> BookingActionResponse:
        self.borrar_calls.append(
            {"class_id": class_id, "ticks": ticks}
        )
        return BookingActionResponse(
            status_code=200,
            latency_ms=1.0,
            outcome="granted",
            raw_res="OkBorrado",
            payload={},
        )


class _StubLoadClass:
    """Shape-compatible LoadClassResponse for the parser.

    The real ``extract_class_slots`` reads ``payload["Data"]`` and
    walks ``Valores[j]["Valor"]``; we mirror that with the one slot
    the tests need to match.
    """

    def __init__(self, slot_id: int, class_type: str, class_time: str) -> None:
        self.payload: dict[str, Any] = {
            "Data": [
                {
                    "Valores": [
                        {
                            "Valor": {
                                "Id": slot_id,
                                "Nombre": class_type,
                                "HoraComienzo": f"{class_time}:00",
                                "Ocupacion": 5,
                                "Capacidad": 20,
                            }
                        }
                    ]
                }
            ]
        }


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


def _make_operator(engine: Engine, name: str = "Op") -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO operator_profile (display_name) "
                    "VALUES (:n) RETURNING id"
                ),
                {"n": name},
            ).scalar_one()
        )


def _seed_cookie(
    engine: Engine, operator_id: int, cookie: str = ".WBAuth-abc"
) -> CookieStore:
    """Insert a stored cookie so ``cancel_booking`` finds one."""
    import os

    cipher = Cipher(os.urandom(32))
    store = CookieStore(cipher)
    sm = sessionmaker(bind=engine)
    with sm() as session:
        store.save(session, operator_id, cookie, validated_at=datetime.now(tz=UTC))
        session.commit()
    return store


def _seed_granted_booking(
    engine: Engine,
    operator_id: int,
    *,
    target_slot: datetime,
    class_type: str = "WOD",
) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO booking_outcome ("
                    " operator_id, rule_id, target_class, target_slot, "
                    " attempted_at, terminal_status"
                    ") VALUES ("
                    " :op, NULL, :cls, :slot, :attempted, 'granted'"
                    ") RETURNING id"
                ),
                {
                    "op": operator_id,
                    "cls": class_type,
                    "slot": target_slot,
                    "attempted": target_slot - timedelta(days=2),
                },
            ).scalar_one()
        )


# ---------------------------------------------------------------------------
# US7.T1: bulk cancel
# ---------------------------------------------------------------------------


def test_enable_bulk_cancels_only_bookings_inside_range(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    store = _seed_cookie(postgres_engine, op_id)
    client = _StubClient()

    # Three granted bookings on three consecutive days. Vacation
    # covers the first two — the third must stay granted.
    day_one = datetime(2026, 8, 3, 21, 30, tzinfo=UTC)  # Mon
    day_two = datetime(2026, 8, 4, 21, 30, tzinfo=UTC)  # Tue
    day_three = datetime(2026, 8, 5, 21, 30, tzinfo=UTC)  # Wed
    b1 = _seed_granted_booking(postgres_engine, op_id, target_slot=day_one)
    b2 = _seed_granted_booking(postgres_engine, op_id, target_slot=day_two)
    b3 = _seed_granted_booking(postgres_engine, op_id, target_slot=day_three)

    with session_factory() as session:
        vacation_service.enable(
            session,
            operator_id=op_id,
            start_date=day_one,
            end_date=day_two,
            client=client,
            cookie_store=store,
            now=day_one - timedelta(days=1),
        )
        session.commit()

    # WodBuster's borrar was invoked exactly twice (b1, b2), not for b3.
    assert len(client.borrar_calls) == 2

    with session_factory() as session:
        rows = {
            int(o.id): o.terminal_status
            for o in session.execute(select(BookingOutcome)).scalars()
        }
    assert rows[b1] == "cancelled"
    assert rows[b2] == "cancelled"
    assert rows[b3] == "granted"


def test_enable_persists_window_with_normalized_boundaries(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """Start collapses to 00:00, end extends to 23:59:59.999999 UTC."""
    op_id = _make_operator(postgres_engine)
    store = _seed_cookie(postgres_engine, op_id)
    client = _StubClient()

    start = datetime(2026, 8, 3, 14, 12, tzinfo=UTC)  # afternoon
    end = datetime(2026, 8, 5, 6, 45, tzinfo=UTC)  # early morning

    with session_factory() as session:
        window = vacation_service.enable(
            session,
            operator_id=op_id,
            start_date=start,
            end_date=end,
            client=client,
            cookie_store=store,
        )
        session.commit()
        window_id = int(window.id)

    with session_factory() as session:
        persisted = session.get(VacationWindow, window_id)
        assert persisted is not None
        assert persisted.start_date == datetime(2026, 8, 3, 0, 0, tzinfo=UTC)
        # Ceiling is 23:59:59.999999 of the end day.
        assert persisted.end_date.replace(microsecond=0) == datetime(
            2026, 8, 5, 23, 59, 59, tzinfo=UTC
        )


# ---------------------------------------------------------------------------
# US7.T2: skip-guard boundary conditions
# ---------------------------------------------------------------------------


def test_find_covering_window_inclusive_start_boundary(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    store = _seed_cookie(postgres_engine, op_id)
    client = _StubClient()

    with session_factory() as session:
        vacation_service.enable(
            session,
            operator_id=op_id,
            start_date=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
            client=client,
            cookie_store=store,
        )
        session.commit()

    # Target sits at midnight of the start day → covered.
    with session_factory() as session:
        result = find_covering_window(
            session,
            operator_id=op_id,
            target_slot=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
            now=datetime(2026, 8, 2, 12, 0, tzinfo=UTC),
        )
    assert result is not None


def test_find_covering_window_inclusive_end_boundary(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    store = _seed_cookie(postgres_engine, op_id)
    client = _StubClient()

    with session_factory() as session:
        vacation_service.enable(
            session,
            operator_id=op_id,
            start_date=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
            client=client,
            cookie_store=store,
        )
        session.commit()

    # A class at 21:30 on the end day still lands inside the window
    # because end_date extends to 23:59:59.999999.
    with session_factory() as session:
        result = find_covering_window(
            session,
            operator_id=op_id,
            target_slot=datetime(2026, 8, 5, 21, 30, tzinfo=UTC),
            now=datetime(2026, 8, 4, 12, 0, tzinfo=UTC),
        )
    assert result is not None


def test_find_covering_window_exclusive_outside_range(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    store = _seed_cookie(postgres_engine, op_id)
    client = _StubClient()

    with session_factory() as session:
        vacation_service.enable(
            session,
            operator_id=op_id,
            start_date=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
            client=client,
            cookie_store=store,
        )
        session.commit()

    # A class the day *after* the end date is not covered — the
    # scheduler resumes normal operation.
    with session_factory() as session:
        result = find_covering_window(
            session,
            operator_id=op_id,
            target_slot=datetime(2026, 8, 6, 21, 30, tzinfo=UTC),
            now=datetime(2026, 8, 5, 12, 0, tzinfo=UTC),
        )
    assert result is None


def test_find_covering_window_ignores_closed_windows(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    store = _seed_cookie(postgres_engine, op_id)
    client = _StubClient()

    with session_factory() as session:
        window = vacation_service.enable(
            session,
            operator_id=op_id,
            start_date=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
            client=client,
            cookie_store=store,
        )
        session.commit()
        window_id = int(window.id)

    with session_factory() as session:
        vacation_service.close_early(
            session,
            operator_id=op_id,
            window_id=window_id,
            now=datetime(2026, 8, 3, 15, 0, tzinfo=UTC),
        )
        session.commit()

    with session_factory() as session:
        result = find_covering_window(
            session,
            operator_id=op_id,
            target_slot=datetime(2026, 8, 4, 21, 30, tzinfo=UTC),
            now=datetime(2026, 8, 3, 16, 0, tzinfo=UTC),
        )
    assert result is None


def test_find_covering_window_scoped_to_operator(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """Another operator's vacation window does not shield this one."""
    op_a = _make_operator(postgres_engine, name="A")
    op_b = _make_operator(postgres_engine, name="B")
    store = _seed_cookie(postgres_engine, op_a)
    client = _StubClient()

    with session_factory() as session:
        vacation_service.enable(
            session,
            operator_id=op_a,
            start_date=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
            client=client,
            cookie_store=store,
        )
        session.commit()

    with session_factory() as session:
        result = find_covering_window(
            session,
            operator_id=op_b,
            target_slot=datetime(2026, 8, 4, 21, 30, tzinfo=UTC),
            now=datetime(2026, 8, 3, 12, 0, tzinfo=UTC),
        )
    assert result is None


# ---------------------------------------------------------------------------
# US7.T3: auto-resume once the window has closed by wall-clock
# ---------------------------------------------------------------------------


def test_expired_window_does_not_shield_future_bookings(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    """Once ``now`` passes ``end_date`` the window falls out of the
    covering set — no manual close needed."""
    op_id = _make_operator(postgres_engine)
    store = _seed_cookie(postgres_engine, op_id)
    client = _StubClient()

    with session_factory() as session:
        vacation_service.enable(
            session,
            operator_id=op_id,
            start_date=datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
            end_date=datetime(2026, 8, 5, 0, 0, tzinfo=UTC),
            client=client,
            cookie_store=store,
        )
        session.commit()

    with session_factory() as session:
        # ``now`` is Aug 6 -> the (Aug 3-5) window is expired.
        result = find_covering_window(
            session,
            operator_id=op_id,
            target_slot=datetime(2026, 8, 6, 21, 30, tzinfo=UTC),
            now=datetime(2026, 8, 6, 12, 0, tzinfo=UTC),
        )
    assert result is None
