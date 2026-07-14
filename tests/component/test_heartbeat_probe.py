"""Component tests for :class:`HeartbeatProbe` (US4.1 slice).

Uses the real Postgres schema via the ``postgres_engine`` fixture from
``conftest.py`` so we can assert the transactional contract end-to-end:
one ``heartbeat_reading`` row per probe, the ``cookie_credential``
freshness columns update in the same transaction, and the projection
respects the estimator's monotonicity rules across cycles.

The :class:`CookieValidator` is replaced with a scripted fake so the
tests never hit the real WodBuster subdomain. Alert emission is out of
scope for this slice.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.heartbeat.probe import (
    HeartbeatOutcome,
    HeartbeatProbe,
    NoCookieOnFile,
)
from wodbuster_worker.persistence.cookie_store import CookieStore
from wodbuster_worker.persistence.models import CookieCredential, HeartbeatReading
from wodbuster_worker.security.cipher import Cipher
from wodbuster_worker.security.cookie import (
    Rejected,
    Unknown,
    Valid,
    ValidationResult,
)

_CEILING = timedelta(days=30)


class _ScriptedValidator:
    """Fake :class:`CookieValidator` that hands out a preset verdict."""

    def __init__(self, verdict: ValidationResult) -> None:
        self._verdict = verdict
        self.calls: list[str] = []

    def validate(self, cookie_value: str) -> ValidationResult:
        self.calls.append(cookie_value)
        return self._verdict


@pytest.fixture
def session_factory(postgres_engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(
        bind=postgres_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


def _make_operator(engine: Engine, name: str = "Alice") -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text("INSERT INTO operator_profile (display_name) VALUES (:n) RETURNING id"),
                {"n": name},
            ).scalar_one()
        )


def _seed_cookie(session: Session, store: CookieStore, operator_id: int, value: str) -> None:
    store.save(session, operator_id, value, validated_at=datetime.now(tz=UTC))
    session.commit()


def _probe(verdict: ValidationResult) -> tuple[HeartbeatProbe, _ScriptedValidator, CookieStore]:
    """Build a probe wired to a scripted validator and a real cipher."""
    import os

    cipher = Cipher(os.urandom(32))
    store = CookieStore(cipher)
    validator = _ScriptedValidator(verdict)
    probe = HeartbeatProbe(store, validator, ceiling=_CEILING)  # type: ignore[arg-type]
    return probe, validator, store


def test_valid_probe_writes_reading_and_updates_freshness_columns(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    probe, validator, store = _probe(Valid(probed_at=datetime.now(tz=UTC)))

    with session_factory() as session:
        _seed_cookie(session, store, op_id, ".WBAuth-x")

    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        outcome = probe.run(session, op_id, now=now)
        session.commit()

    assert isinstance(outcome, HeartbeatOutcome)
    assert outcome.result == "valid"
    assert outcome.probed_at == now
    assert outcome.projected_ttl_at == now + _CEILING
    assert validator.calls == [".WBAuth-x"]
    assert outcome.reading_id > 0

    with session_factory() as session:
        cred = session.query(CookieCredential).filter_by(operator_id=op_id).one()
        assert cred.last_validated_at == now
        assert cred.last_probe_status == "valid"
        assert cred.projected_ttl_at == now + _CEILING

        readings = session.query(HeartbeatReading).filter_by(operator_id=op_id).all()
        assert len(readings) == 1
        assert readings[0].result == "valid"
        assert readings[0].projected_ttl_at == now + _CEILING
        assert readings[0].alert_id is None  # slice 3 will populate


def test_rejected_probe_forces_immediate_expiry_projection(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    probe, _, store = _probe(Rejected(reason="server rejected"))

    with session_factory() as session:
        _seed_cookie(session, store, op_id, ".WBAuth-x")
        # Simulate a fresh Valid heartbeat's projection so we can prove
        # a Rejected verdict overrides it.
        cred = session.query(CookieCredential).filter_by(operator_id=op_id).one()
        cred.projected_ttl_at = datetime(2026, 8, 1, tzinfo=UTC)
        session.commit()

    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        outcome = probe.run(session, op_id, now=now)
        session.commit()

    assert outcome.result == "rejected"
    assert outcome.projected_ttl_at == now  # immediate expiry

    with session_factory() as session:
        cred = session.query(CookieCredential).filter_by(operator_id=op_id).one()
        assert cred.projected_ttl_at == now
        assert cred.last_probe_status == "rejected"


def test_unknown_probe_leaves_projection_unchanged(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    probe, _, store = _probe(Unknown(reason="transport error"))

    original_projection = datetime(2026, 8, 15, tzinfo=UTC)
    with session_factory() as session:
        _seed_cookie(session, store, op_id, ".WBAuth-x")
        cred = session.query(CookieCredential).filter_by(operator_id=op_id).one()
        cred.projected_ttl_at = original_projection
        session.commit()

    now = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        outcome = probe.run(session, op_id, now=now)
        session.commit()

    assert outcome.result == "unknown"
    # Critical invariant: Unknown must NOT touch the projection.
    assert outcome.projected_ttl_at == original_projection

    with session_factory() as session:
        cred = session.query(CookieCredential).filter_by(operator_id=op_id).one()
        assert cred.projected_ttl_at == original_projection
        assert cred.last_probe_status == "unknown"
        # ``last_validated_at`` still updates so the "we ran a probe"
        # signal is visible even when the verdict was inconclusive.
        assert cred.last_validated_at == now


def test_run_without_cookie_raises_and_writes_nothing(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    probe, validator, _ = _probe(Valid(probed_at=datetime.now(tz=UTC)))

    with session_factory() as session, pytest.raises(NoCookieOnFile):
        probe.run(session, op_id)

    assert validator.calls == []  # no probe issued
    with session_factory() as session:
        assert session.query(HeartbeatReading).filter_by(operator_id=op_id).count() == 0


def test_consecutive_valid_probes_are_monotonic_non_increasing(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    op_id = _make_operator(postgres_engine)
    probe, _, store = _probe(Valid(probed_at=datetime.now(tz=UTC)))

    with session_factory() as session:
        _seed_cookie(session, store, op_id, ".WBAuth-x")

    day_0 = datetime(2026, 7, 8, 12, 0, tzinfo=UTC)
    with session_factory() as session:
        outcome_0 = probe.run(session, op_id, now=day_0)
        session.commit()

    day_1 = day_0 + timedelta(days=1)
    with session_factory() as session:
        outcome_1 = probe.run(session, op_id, now=day_1)
        session.commit()

    # Day 0's ceiling was day_0 + 30d. Day 1's Valid probe suggests
    # day_1 + 30d, which is one day LATER — but the estimator refuses
    # to move the projection forward, so day 1 keeps day 0's value.
    assert outcome_0.projected_ttl_at == day_0 + _CEILING
    assert outcome_1.projected_ttl_at == day_0 + _CEILING


def test_probe_writes_reading_row_even_for_unknown_verdict(
    postgres_engine: Engine, session_factory: sessionmaker[Session]
) -> None:
    # The audit-trail contract: every probe writes exactly one
    # heartbeat_reading row, regardless of verdict.
    op_id = _make_operator(postgres_engine)
    probe, _, store = _probe(Unknown(reason="server 502"))

    with session_factory() as session:
        _seed_cookie(session, store, op_id, ".WBAuth-x")

    with session_factory() as session:
        probe.run(session, op_id)
        session.commit()

    with session_factory() as session:
        readings = session.query(HeartbeatReading).filter_by(operator_id=op_id).all()
        assert len(readings) == 1
        assert readings[0].result == "unknown"
