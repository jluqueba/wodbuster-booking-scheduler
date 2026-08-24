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

import json
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from wodbuster_worker.booking.outcomes import persist_outcome
from wodbuster_worker.persistence.models import (
    Alert,
    BookingOutcome,
    NotificationOutbox,
)

from .conftest import gym_account_id_for


def _seed_operator(
    engine: Engine,
    *,
    telegram_chat_id: str | None = None,
    email: str | None = None,
    email_preferences: dict[str, bool] | None = None,
) -> int:
    with engine.begin() as conn:
        op_id = int(
            conn.execute(
                text(
                    "INSERT INTO operator_profile "
                    "(display_name, telegram_chat_id, email, email_preferences) "
                    "VALUES (:n, :tg, :email, COALESCE(CAST(:prefs AS jsonb), '{}'::jsonb)) "
                    "RETURNING id"
                ),
                {
                    "n": "Op",
                    "tg": telegram_chat_id,
                    "email": email,
                    "prefs": json.dumps(email_preferences) if email_preferences else None,
                },
            ).scalar_one()
        )
        conn.execute(
            text(
                "INSERT INTO gym_account (user_id, gym_slug, display_name, idu) "
                "VALUES (:op, 'antworktrainingcenter', :n, :idu)"
            ),
            {"op": op_id, "n": "Op", "idu": f"idu{op_id:032d}"[:32]},
        )
        return op_id


def _seed_rule(engine: Engine, *, operator_id: int) -> int:
    with engine.begin() as conn:
        gym_account_id = gym_account_id_for(conn, operator_id)
        return int(
            conn.execute(
                text(
                    "INSERT INTO scheduler_rule "
                    "(gym_account_id, day_of_week, class_type, class_time, "
                    "booking_opens_days_before, booking_opens_at, active) "
                    "VALUES (:op, 2, 'WOD', '21:30', 2, '21:30', true) "
                    "RETURNING id"
                ),
                {"op": gym_account_id},
            ).scalar_one()
        )


def _gym_account_id(engine: Engine, operator_id: int) -> int:
    with engine.connect() as conn:
        return gym_account_id_for(conn, operator_id)


def _session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)


def test_granted_outcome_writes_row_and_banner_only_when_no_chat_id(
    postgres_engine: Engine,
) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id=None)
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            gym_account_id=ga_id,
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
        rows = session.query(BookingOutcome).filter_by(gym_account_id=ga_id).all()
        assert len(rows) == 1
        outcome = rows[0]
        assert outcome.terminal_status == "granted"
        assert outcome.granted_fallback_index == 0
        assert outcome.target_class == "WOD"

        outbox = session.query(NotificationOutbox).filter_by(user_id=op_id).all()
        # Only the banner row — Telegram is skipped without chat_id.
        assert len(outbox) == 1
        assert outbox[0].kind == "banner"
        payload = outbox[0].payload
        assert payload["kind"] == "booking_result"
        assert payload["terminal_status"] == "granted"
        assert payload["text"] == "Booked WOD."
        assert payload["outcome_id"] == outcome.id

        # No alert row — granted is not a persistent condition.
        assert session.query(Alert).filter_by(gym_account_id=ga_id).count() == 0


def test_granted_with_chat_id_writes_both_channels(postgres_engine: Engine) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-999")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            gym_account_id=ga_id,
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
        outbox = session.query(NotificationOutbox).filter_by(user_id=op_id).all()
        assert {row.kind for row in outbox} == {"banner", "telegram"}
        telegram_row = next(row for row in outbox if row.kind == "telegram")
        assert telegram_row.target == "tg-999"


def test_full_outcome_persists_without_alert(postgres_engine: Engine) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            gym_account_id=ga_id,
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
        assert session.query(Alert).filter_by(gym_account_id=ga_id).count() == 0


def test_cookie_invalid_outcome_opens_alert(postgres_engine: Engine) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            gym_account_id=ga_id,
            rule_id=rule_id,
            target_class="WOD",
            target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
            terminal_status="cookie_invalid",
            response_payload="Res='SinAcceso'",
            telegram_text="Cookie is invalid.",
        )
        session.commit()

    with factory() as session:
        alerts = session.query(Alert).filter_by(gym_account_id=ga_id).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.kind == "cookie_invalid"
        assert alert.closed_at is None


def test_repeated_cookie_invalid_refreshes_open_alert_without_duplicating(
    postgres_engine: Engine,
) -> None:
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)

    first_now = datetime(2026, 7, 15, 21, 30, tzinfo=UTC)
    second_now = datetime(2026, 7, 15, 22, 30, tzinfo=UTC)

    with factory() as session:
        persist_outcome(
            session,
            gym_account_id=ga_id,
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
            gym_account_id=ga_id,
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
        alerts = session.query(Alert).filter_by(gym_account_id=ga_id).all()
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
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            gym_account_id=ga_id,
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


# ---------------------------------------------------------------------------
# Single-day override fallback (T-BDO-014, ADR-0012 Decision 5)
# ---------------------------------------------------------------------------


def _persist_fallback_granted(
    session: Session,
    *,
    gym_account_id: int,
    rule_id: int,
    now: datetime,
    target_class: str = "WOD",
) -> None:
    """Persist one ``granted`` outcome sourced to the override fallback."""
    persist_outcome(
        session,
        gym_account_id=gym_account_id,
        rule_id=rule_id,
        target_class=target_class,
        target_slot=now,
        terminal_status="granted",
        outcome_source="override_fallback",
        granted_fallback_index=0,
        response_payload="Res='Ok'",
        telegram_text=f"Booked {target_class}.",
        requested_class="Halterofilia",
        requested_time="19:00",
        fallback_reason="class_not_visible",
        now=now,
    )


def test_fallback_granted_opens_alert_naming_booked_and_requested_class(
    postgres_engine: Engine,
) -> None:
    """FR-015, INV-008: the fallback banner carries everything a message
    needs (booked class, requested class, reason) without a second query."""
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)
    now = datetime(2026, 7, 15, 21, 30, tzinfo=UTC)

    with factory() as session:
        _persist_fallback_granted(session, gym_account_id=ga_id, rule_id=rule_id, now=now)
        session.commit()

    with factory() as session:
        alerts = session.query(Alert).filter_by(gym_account_id=ga_id).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.kind == "booking_fallback"
        assert alert.closed_at is None
        assert alert.payload["class_type"] == "WOD"
        assert alert.payload["requested_class"] == "Halterofilia"
        assert alert.payload["requested_time"] == "19:00"
        assert alert.payload["fallback_reason"] == "class_not_visible"

        # The same detail rides the outbox payload, so Telegram and email
        # render from the row rather than re-reading the override.
        telegram_row = session.query(NotificationOutbox).filter_by(kind="telegram").one()
        assert telegram_row.payload["outcome_source"] == "override_fallback"
        assert telegram_row.payload["requested_class"] == "Halterofilia"


def test_second_fallback_refreshes_the_same_alert_row(postgres_engine: Engine) -> None:
    """ADR-0012 accepted limitation: the partial unique index on ``alert``
    collapses two fallbacks in the same period into one refreshed row."""
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)
    first_now = datetime(2026, 7, 15, 21, 30, tzinfo=UTC)
    second_now = datetime(2026, 7, 16, 21, 30, tzinfo=UTC)

    with factory() as session:
        _persist_fallback_granted(session, gym_account_id=ga_id, rule_id=rule_id, now=first_now)
        session.commit()

    with factory() as session:
        _persist_fallback_granted(
            session,
            gym_account_id=ga_id,
            rule_id=rule_id,
            now=second_now,
            target_class="Gimnasia",
        )
        session.commit()

    with factory() as session:
        alerts = session.query(Alert).filter_by(gym_account_id=ga_id).all()
        assert len(alerts) == 1
        alert = alerts[0]
        assert alert.first_emitted_at.replace(tzinfo=UTC) == first_now
        assert alert.last_emitted_at.replace(tzinfo=UTC) == second_now
        # The banner describes the most recent event; the per-event detail
        # lives on the two outcome rows.
        assert alert.payload["class_type"] == "Gimnasia"
        assert session.query(BookingOutcome).count() == 2


def test_fallback_alert_opens_even_with_the_bookings_email_category_off(
    postgres_engine: Engine,
) -> None:
    """The banner is not a user-toggleable channel, so a disabled email
    category removes the email row and leaves the alert standing."""
    op_id = _seed_operator(
        postgres_engine,
        telegram_chat_id="tg-1",
        email="op@example.com",
        email_preferences={"bookings": False},
    )
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)
    now = datetime(2026, 7, 15, 21, 30, tzinfo=UTC)

    with factory() as session:
        _persist_fallback_granted(session, gym_account_id=ga_id, rule_id=rule_id, now=now)
        session.commit()

    with factory() as session:
        kinds = {row.kind for row in session.query(NotificationOutbox).all()}
        assert kinds == {"banner", "telegram"}
        assert session.query(Alert).filter_by(kind="booking_fallback").count() == 1


def test_fallback_alert_opens_even_without_a_telegram_chat(postgres_engine: Engine) -> None:
    """Same guarantee on the other channel: an unbound chat enqueues no
    Telegram row and the alert is still opened."""
    op_id = _seed_operator(postgres_engine, telegram_chat_id=None)
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)
    now = datetime(2026, 7, 15, 21, 30, tzinfo=UTC)

    with factory() as session:
        _persist_fallback_granted(session, gym_account_id=ga_id, rule_id=rule_id, now=now)
        session.commit()

    with factory() as session:
        kinds = {row.kind for row in session.query(NotificationOutbox).all()}
        assert kinds == {"banner"}
        assert session.query(Alert).filter_by(kind="booking_fallback").count() == 1


def test_exhausted_fallback_records_the_source_without_opening_an_alert(
    postgres_engine: Engine,
) -> None:
    """FR-016: an exhausted chain is a plain failure. The fallback banner is
    reserved for a substituted booking, which is the case FR-015 covers."""
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            gym_account_id=ga_id,
            rule_id=rule_id,
            target_class="WOD",
            target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
            terminal_status="class_not_visible",
            outcome_source="override_fallback",
            response_payload="not visible",
            telegram_text="Could not book WOD.",
            requested_class="Halterofilia",
            requested_time="19:00",
            fallback_reason="class_not_visible",
        )
        session.commit()

    with factory() as session:
        outcome = session.query(BookingOutcome).one()
        assert outcome.outcome_source == "override_fallback"
        assert session.query(Alert).count() == 0


def test_plain_rule_success_carries_no_fallback_payload(postgres_engine: Engine) -> None:
    """Regression guard: the new keys stay off the payload of every outcome
    the override feature did not touch."""
    op_id = _seed_operator(postgres_engine, telegram_chat_id="tg-1")
    rule_id = _seed_rule(postgres_engine, operator_id=op_id)
    ga_id = _gym_account_id(postgres_engine, op_id)
    factory = _session_factory(postgres_engine)

    with factory() as session:
        persist_outcome(
            session,
            gym_account_id=ga_id,
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
        payload = session.query(NotificationOutbox).filter_by(kind="banner").one().payload
        assert payload["outcome_source"] == "rule"
        assert "requested_class" not in payload
        assert "requested_time" not in payload
        assert "fallback_reason" not in payload
        assert session.query(Alert).count() == 0
