"""Component tests for :func:`persist_outcome` (US1.8).

Validates the transactional contract against a real Postgres schema:

- ``booking_outcome`` row + paired ``notification_outbox`` row(s) are
  visible after commit.
- Telegram outbox row is skipped when the operator has no
  ``telegram_chat_id`` on file.
- ``cookie_invalid`` terminal opens (or refreshes) the operator's
  open ``cookie_invalid`` alert so the dashboard banner surfaces the
  persistent condition.
- Repeated ``cookie_invalid`` outcomes do NOT create duplicate open
  alerts — the partial unique index on ``alert`` forbids that.
- Rolling back the session rolls back the outbox row too (contract
  proof: no operator-visible signal for an unpersisted outcome).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import sessionmaker

from wodbuster_worker.booking.outcomes import persist_outcome
from wodbuster_worker.persistence.models import (
    Alert,
    BookingOutcome,
    NotificationOutbox,
)


def _seed_operator(engine: Engine, *, telegram_chat_id: str | None = None) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO operator_profile (display_name, telegram_chat_id) "
                    "VALUES (:n, :tg) RETURNING id"
                ),
                {"n": "Op", "tg": telegram_chat_id},
            ).scalar_one()
        )


def _seed_rule(engine: Engine, *, operator_id: int) -> int:
    with engine.begin() as conn:
        return int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule "
                    "(operator_id, day_of_week, class_type, class_time, "
                    "booking_opens_days_before, booking_opens_at, active) "
                    "VALUES (:op, 2, 'WOD', '21:30', 2, '21:30', true) "
                    "RETURNING id"
                ),
                {"op": operator_id},
            ).scalar_one()
        )


def _session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def test_granted_outcome_writes_row_and_banner_only_when_no_chat_id(
    postgres_engine: Engine,
) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id=None)
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            operator_id=op_id,
            rule_id=rule_id,
            target_class="WOD",
            target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
            terminal_status="granted",
            granted_fallback_index=0,
            response_payload="Res='Ok' keys=[...]",
            telegram_text="Booked WOD.",
        )
        session.commit()

    with factory() as session:
        rows = session.query(BookingOutcome).filter_by(operator_id=op_id).all()
        assert len(rows) == 1
        outcome = rows[0]
        assert outcome.terminal_status == "granted"
        assert outcome.granted_fallback_index == 0
        assert outcome.target_class == "WOD"

        outbox = session.query(NotificationOutbox).filter_by(operator_id=op_id).all()
        # Only the banner row — Telegram is skipped without chat_id.
        assert len(outbox) == 1
        assert outbox[0].kind == "banner"
        payload = outbox[0].payload
        assert payload["kind"] == "booking_result"
        assert payload["terminal_status"] == "granted"
        assert payload["text"] == "Booked WOD."
        assert payload["outcome_id"] == outcome.id

        # No alert row — granted is not a persistent condition.
        assert session.query(Alert).filter_by(operator_id=op_id).count() == 0


def test_granted_with_chat_id_writes_both_channels(postgres_engine: Engine) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-999")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            operator_id=op_id,
            rule_id=rule_id,
            target_class="WOD",
            target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
            terminal_status="granted",
            granted_fallback_index=0,
            response_payload="Res='Ok'",
            telegram_text="Booked WOD.",
        )
        session.commit()

    with factory() as session:
        outbox = session.query(NotificationOutbox).filter_by(operator_id=op_id).all()
        assert {row.kind for row in outbox} == {"banner", "telegram"}
        telegram_row = next(row for row in outbox if row.kind == "telegram")
        assert telegram_row.target == "tg-999"


def test_full_outcome_persists_without_alert(postgres_engine: Engine) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            operator_id=op_id,
            rule_id=rule_id,
            target_class="WOD",
            target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
            terminal_status="full",
            response_payload="Res='Completa'",
            telegram_text="Could not book WOD: class was full.",
        )
        session.commit()

    with factory() as session:
        outcome = session.query(BookingOutcome).one()
        assert outcome.terminal_status == "full"
        assert outcome.granted_fallback_index is None
        # No alert row — a full class is not a persistent condition.
        assert session.query(Alert).filter_by(operator_id=op_id).count() == 0


def test_cookie_invalid_outcome_opens_alert(postgres_engine: Engine) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            operator_id=op_id,
            rule_id=rule_id,
            target_class="WOD",
            target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
            terminal_status="cookie_invalid",
            response_payload="Res='SinAcceso'",
            telegram_text="Cookie is invalid.",
        )
        session.commit()

    with factory() as session:
        alerts = session.query(Alert).filter_by(operator_id=op_id).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.kind == "cookie_invalid"
        assert alert.closed_at is None


def test_repeated_cookie_invalid_refreshes_open_alert_without_duplicating(
    postgres_engine: Engine,
) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    factory = _session_factory(postgres_engine)

    first_now = datetime(2026, 7, 15, 21, 30, tzinfo=UTC)
    second_now = datetime(2026, 7, 15, 22, 30, tzinfo=UTC)

    with factory() as session:
        persist_outcome(
            session,
            operator_id=op_id,
            rule_id=rule_id,
            target_class="WOD",
            target_slot=first_now,
            terminal_status="cookie_invalid",
            response_payload="first",
            telegram_text="Cookie invalid.",
            now=first_now,
        )
        session.commit()

    with factory() as session:
        persist_outcome(
            session,
            operator_id=op_id,
            rule_id=rule_id,
            target_class="WOD",
            target_slot=second_now,
            terminal_status="cookie_invalid",
            response_payload="second",
            telegram_text="Cookie invalid.",
            now=second_now,
        )
        session.commit()

    with factory() as session:
        alerts = session.query(Alert).filter_by(operator_id=op_id).all()
        # Single alert row (partial unique index on open+kind).
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.first_emitted_at.replace(tzinfo=UTC) == first_now
        assert alert.last_emitted_at.replace(tzinfo=UTC) == second_now


def test_rollback_undoes_outcome_and_outbox_together(
    postgres_engine: Engine,
) -> None:
    """The plan cross-cutting rule: outbox + entity share a transaction."""
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            operator_id=op_id,
            rule_id=rule_id,
            target_class="WOD",
            target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
            terminal_status="granted",
            granted_fallback_index=0,
            response_payload="Res='Ok'",
            telegram_text="Booked WOD.",
        )
        session.rollback()

    with factory() as session:
        assert session.query(BookingOutcome).count() == 0
        assert session.query(NotificationOutbox).count() == 0
        assert session.query(Alert).count() == 0
