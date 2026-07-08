"""Component tests for heartbeat-driven alert emission (US4.T3, US4.T5).

Exercises the full alert path against real Postgres: probe → alert
evaluator → alert / outbox rows. Also covers the clear-on-refresh
flow (US4.4) via :func:`close_open_cookie_expiring`.

The tests pin the projection deterministically by overwriting
``cookie_credential.projected_ttl_at`` after each seed / cycle. The
estimator's ``min(previous, now + ceiling)`` rule then locks the value
in place for the next probe, keeping the alert-band arithmetic
readable rather than needing a mocked estimator.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.heartbeat.alerts import (
    apply_alert_action,
    close_open_cookie_expiring,
    evaluate_cookie_expiring,
    previous_heartbeat_at,
)
from wodbuster_worker.heartbeat.probe import HeartbeatProbe
from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.persistence.models import (
    Alert,
    HeartbeatReading,
    NotificationOutbox,
)
from wodbuster_worker.security.cipher import Cipher
from wodbuster_worker.security.cookie import Valid, ValidationResult

_CEILING = timedelta(days=30)

# Rule anchor: Wednesday 21:30 UTC class, 48h before-window offset.
# Result: window opens on **Monday** 21:30 UTC of the same calendar week.
# All scenarios below pick ``now = 2026-07-06 (Mon) 09:30 UTC`` so the
# next window is 12h away — inside the 24h alert band.
_NOW = datetime(2026, 7, 6, 9, 30, tzinfo=UTC)
_NEXT_WINDOW = datetime(2026, 7, 6, 21, 30, tzinfo=UTC)
_INSIDE_BAND_TTL = datetime(2026, 7, 6, 15, 30, tzinfo=UTC)  # 6h from now


class _ScriptedValidator:
    """Fake validator — always hands back the same verdict."""

    def __init__(self, verdict: ValidationResult) -> None:
        self.verdict = verdict

    def validate(self, cookie_value: str) -> ValidationResult:
        return self.verdict


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def _make_operator(engine: Engine, *, telegram_chat_id: str | None = None) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO operator_profile "
                    "(display_name, telegram_chat_id) "
                    "VALUES (:n, :tg) RETURNING id"
                ),
                {"n": "Op", "tg": telegram_chat_id},
            ).scalar_one()
        )


def _make_wednesday_rule(engine: Engine, operator_id: int) -> None:
    """Insert the Wed-21:30 / 48h-offset rule for the operator."""
    with engine.begin() as conn:
        rule_id = int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule "
                    "(operator_id, day_of_week, window_offset_hours, active) "
                    "VALUES (:op, 2, 48, true) RETURNING id"
                ),
                {"op": operator_id},
            ).scalar_one()
        )
        conn.execute(
            text(
                "INSERT INTO class_preference "
                "(rule_id, order_index, class_type, target_time_slot) "
                "VALUES (:r, 0, 'WOD', '21:30')"
            ),
            {"r": rule_id},
        )


def _seed_cookie(session_factory: sessionmaker[Session], operator_id: int) -> Cipher:
    cipher = Cipher(os.urandom(32))
    store = CookieStore(cipher)
    with session_factory() as session:
        store.save(
            session,
            operator_id,
            ".WBAuth-x",
            validated_at=datetime.now(tz=UTC),
        )
        session.commit()
    return cipher


def _pin_projection(
    session_factory: sessionmaker[Session],
    operator_id: int,
    projected_ttl_at: datetime,
) -> None:
    with session_factory() as session:
        session.execute(
            text(
                "UPDATE cookie_credential SET projected_ttl_at = :ttl "
                "WHERE operator_id = :op"
            ),
            {"ttl": projected_ttl_at, "op": operator_id},
        )
        session.commit()


def _build_probe(cipher: Cipher) -> HeartbeatProbe:
    store = CookieStore(cipher)
    validator = _ScriptedValidator(Valid(probed_at=datetime.now(tz=UTC)))
    return HeartbeatProbe(store, validator, ceiling=_CEILING)  # type: ignore[arg-type]


def _run_one_cycle(
    session_factory: sessionmaker[Session],
    probe: HeartbeatProbe,
    operator_id: int,
    now: datetime,
) -> int | None:
    with session_factory() as session:
        outcome = probe.run(session, operator_id, now=now)
        prev_at = previous_heartbeat_at(session, operator_id, outcome.probed_at)
        action = evaluate_cookie_expiring(
            session=session,
            operator_id=operator_id,
            projected_ttl_at=outcome.projected_ttl_at,
            now=outcome.probed_at,
            previous_heartbeat_at=prev_at,
        )
        alert_id = apply_alert_action(
            session, operator_id, action, now=outcome.probed_at
        )
        session.commit()
        return alert_id


@pytest.fixture
def scenario(
    postgres_engine: Engine,
    session_factory: sessionmaker[Session],
) -> Callable[..., tuple[int, HeartbeatProbe]]:
    """Return a factory that sets up an operator ready to alert.

    The operator has a Wednesday rule, a cookie on file, and a
    projection pinned inside the alert band. The caller drives cycles
    by calling :func:`_run_one_cycle` directly.
    """

    def build(*, telegram_chat_id: str | None = "42") -> tuple[int, HeartbeatProbe]:
        op_id = _make_operator(postgres_engine, telegram_chat_id=telegram_chat_id)
        _make_wednesday_rule(postgres_engine, op_id)
        cipher = _seed_cookie(session_factory, op_id)
        _pin_projection(session_factory, op_id, _INSIDE_BAND_TTL)
        return op_id, _build_probe(cipher)

    return build


def test_first_emission_creates_alert_and_two_outbox_rows(
    scenario, session_factory: sessionmaker[Session]
) -> None:
    op_id, probe = scenario(telegram_chat_id="123456789")

    alert_id = _run_one_cycle(session_factory, probe, op_id, _NOW)

    assert alert_id is not None
    with session_factory() as session:
        alert = session.get(Alert, alert_id)
        assert alert is not None
        assert alert.closed_at is None
        assert alert.first_emitted_at == _NOW
        assert alert.last_emitted_at == _NOW
        assert alert.payload is not None
        assert alert.payload["kind"] == "cookie_expiring"
        assert alert.payload["next_window_at"] == _NEXT_WINDOW.isoformat()

        outbox = session.query(NotificationOutbox).filter_by(operator_id=op_id).all()
        # Postgres enums sort by declaration order (telegram, banner),
        # not alphabetically. Assert on the *set* of kinds instead.
        by_kind = {row.kind: row for row in outbox}
        assert set(by_kind.keys()) == {"banner", "telegram"}
        assert by_kind["telegram"].target == "123456789"
        for row in outbox:
            assert row.payload["alert_id"] == alert_id


def test_no_telegram_binding_writes_only_banner_row(
    scenario, session_factory: sessionmaker[Session]
) -> None:
    op_id, probe = scenario(telegram_chat_id=None)

    _run_one_cycle(session_factory, probe, op_id, _NOW)

    with session_factory() as session:
        kinds = [
            row.kind
            for row in session.query(NotificationOutbox)
            .filter_by(operator_id=op_id)
            .all()
        ]
        assert kinds == ["banner"]


def test_re_emission_updates_last_emitted_and_appends_outbox_rows(
    scenario, session_factory: sessionmaker[Session]
) -> None:
    op_id, probe = scenario()
    alert_id_1 = _run_one_cycle(session_factory, probe, op_id, _NOW)

    # Estimator locked projected_ttl_at at _INSIDE_BAND_TTL on cycle 1
    # (min of previous and now+ceiling), so cycle 2 sees the same value
    # without re-pinning.
    now_2 = _NOW + timedelta(hours=1)
    alert_id_2 = _run_one_cycle(session_factory, probe, op_id, now_2)

    assert alert_id_1 == alert_id_2  # same open row
    with session_factory() as session:
        alert = session.get(Alert, alert_id_1)
        assert alert is not None
        assert alert.first_emitted_at == _NOW
        assert alert.last_emitted_at == now_2
        # Two cycles x two outbox rows (banner + telegram) = 4 total.
        outbox_count = (
            session.query(NotificationOutbox).filter_by(operator_id=op_id).count()
        )
        assert outbox_count == 4


def test_recent_ack_suppresses_the_next_cycle(
    scenario, session_factory: sessionmaker[Session]
) -> None:
    op_id, probe = scenario()
    alert_id = _run_one_cycle(session_factory, probe, op_id, _NOW)

    # Operator acknowledges between cycles.
    with session_factory() as session:
        alert = session.get(Alert, alert_id)
        assert alert is not None
        alert.acknowledged_at = _NOW + timedelta(minutes=10)
        session.commit()

    now_2 = _NOW + timedelta(hours=1)
    _run_one_cycle(session_factory, probe, op_id, now_2)

    with session_factory() as session:
        outbox_count = (
            session.query(NotificationOutbox).filter_by(operator_id=op_id).count()
        )
        # Cycle 1 wrote 2 outbox rows; cycle 2 Suppresses.
        assert outbox_count == 2
        alert = session.get(Alert, alert_id)
        assert alert is not None
        # last_emitted_at unchanged (Suppress does not touch it).
        assert alert.last_emitted_at == _NOW


def test_projection_recovers_and_open_alert_is_cleared(
    scenario, session_factory: sessionmaker[Session]
) -> None:
    op_id, probe = scenario()
    _run_one_cycle(session_factory, probe, op_id, _NOW)

    # Bump projection well past the window → threshold no longer holds.
    _pin_projection(session_factory, op_id, datetime(2026, 7, 20, tzinfo=UTC))

    now_2 = _NOW + timedelta(hours=1)
    _run_one_cycle(session_factory, probe, op_id, now_2)

    with session_factory() as session:
        alert = session.execute(
            select(Alert).where(
                Alert.operator_id == op_id, Alert.kind == "cookie_expiring"
            )
        ).scalar_one()
        assert alert.closed_at == now_2


def test_close_open_cookie_expiring_on_paste_clears_alert(
    scenario, session_factory: sessionmaker[Session]
) -> None:
    # US4.4: a successful paste closes the open alert in the same
    # transaction so the banner clears immediately.
    op_id, probe = scenario()
    _run_one_cycle(session_factory, probe, op_id, _NOW)

    paste_time = _NOW + timedelta(minutes=5)
    with session_factory() as session:
        closed_id = close_open_cookie_expiring(session, op_id, now=paste_time)
        session.commit()

    assert closed_id is not None
    with session_factory() as session:
        alert = session.execute(
            select(Alert).where(
                Alert.operator_id == op_id, Alert.kind == "cookie_expiring"
            )
        ).scalar_one()
        assert alert.closed_at == paste_time


def test_close_open_cookie_expiring_is_noop_when_none_open(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)

    with session_factory() as session:
        result = close_open_cookie_expiring(session, op_id, now=datetime.now(tz=UTC))
        session.commit()

    assert result is None


def test_no_rule_produces_no_alert(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine, telegram_chat_id="42")
    cipher = _seed_cookie(session_factory, op_id)
    _pin_projection(session_factory, op_id, _INSIDE_BAND_TTL)
    probe = _build_probe(cipher)

    alert_id = _run_one_cycle(session_factory, probe, op_id, _NOW)

    assert alert_id is None
    with session_factory() as session:
        assert session.query(Alert).filter_by(operator_id=op_id).count() == 0
        assert (
            session.query(NotificationOutbox).filter_by(operator_id=op_id).count() == 0
        )


def test_heartbeat_reading_row_is_written_alongside_alert(
    scenario, session_factory: sessionmaker[Session]
) -> None:
    op_id, probe = scenario()
    _run_one_cycle(session_factory, probe, op_id, _NOW)

    with session_factory() as session:
        readings = session.query(HeartbeatReading).filter_by(operator_id=op_id).all()
        assert len(readings) == 1
        # ``alert_id`` on the reading stays null in this slice; a later
        # refactor may backfill it when a "readings that produced
        # alerts" query becomes useful.
        assert readings[0].alert_id is None
