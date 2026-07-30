"""Unit tests for send-time Telegram message rendering (T-UP-009a).

Pure (no DB, no network): the renderer maps a structured outbox
payload to a localised body. Verifies the three consistency
guarantees agreed 2026-07-30 — one date format, the gym name, and the
booking ``#id`` — plus that the language follows the argument, not a
request contextvar.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wodbuster_worker.notifications import messages


@pytest.fixture(autouse=True)
def _fixed_timezone(monkeypatch: pytest.MonkeyPatch) -> None:
    # Pin the gym timezone so the formatted slot is deterministic.
    monkeypatch.setenv("WORKER_TIMEZONE", "UTC")


def test_format_slot_is_localised_and_consistent() -> None:
    when = datetime(2026, 7, 10, 18, 30, tzinfo=UTC)  # a Friday
    assert messages.format_slot(when, "en") == "Fri 10 Jul 18:30"
    assert messages.format_slot(when, "es") == "vie 10 jul 18:30"


def test_booking_granted_carries_gym_id_and_date_in_each_language() -> None:
    payload = {
        "kind": "booking_result",
        "terminal_status": "granted",
        "outcome_id": 42,
        "class_type": "WOD",
        "target_slot": "2026-07-10T18:30+00:00",
    }
    english = messages.render(payload, lang="en", gym_name="Ant Work")
    spanish = messages.render(payload, lang="es", gym_name="Ant Work")

    assert english is not None and spanish is not None
    for body in (english, spanish):
        assert "[Ant Work]" in body  # gym name
        assert "#42" in body  # booking id
        assert "WOD" in body  # class
    assert "Booked" in english
    assert "10 Jul 18:30" in english
    assert "Reservado" in spanish
    assert "10 jul 18:30" in spanish


def test_booking_cancelled_uses_cancel_copy() -> None:
    payload = {
        "kind": "booking_result",
        "terminal_status": "cancelled",
        "outcome_id": 7,
        "class_type": "Gymnastics",
        "target_slot": "2026-07-10T18:30+00:00",
    }
    body = messages.render(payload, lang="es", gym_name="Demo Gym")
    assert body is not None
    assert body.startswith("\U0001f6ab [Demo Gym] Cancelada #7:")


def test_unknown_terminal_status_falls_back_to_generic_line() -> None:
    payload = {
        "kind": "booking_result",
        "terminal_status": "weird_new_status",
        "outcome_id": 9,
        "class_type": "WOD",
        "target_slot": "2026-07-10T18:30+00:00",
    }
    body = messages.render(payload, lang="en", gym_name="Ant Work")
    assert body is not None
    assert "#9" in body
    assert "weird_new_status" in body


def test_cookie_expiring_renders_window_date() -> None:
    payload = {
        "kind": "cookie_expiring",
        "next_window_at": "2026-07-10T18:30+00:00",
    }
    body = messages.render(payload, lang="en", gym_name="Ant Work")
    assert body is not None
    assert "[Ant Work]" in body
    assert "10 Jul 18:30" in body


def test_anomaly_singular_vs_plural() -> None:
    one = messages.render(
        {
            "kind": "heartbeat_anomaly",
            "missed": [{"target_class": "WOD", "target_slot": "2026-07-10T18:30+00:00"}],
        },
        lang="en",
        gym_name="Ant Work",
    )
    many = messages.render(
        {
            "kind": "heartbeat_anomaly",
            "missed": [
                {"target_class": "WOD", "target_slot": "2026-07-10T18:30+00:00"},
                {"target_class": "Row", "target_slot": "2026-07-11T18:30+00:00"},
            ],
        },
        lang="en",
        gym_name="Ant Work",
    )
    assert one is not None and "WOD" in one and "10 Jul 18:30" in one
    assert many is not None and "2 scheduled bookings" in many


def test_unknown_kind_returns_none_for_text_fallback() -> None:
    assert messages.render({"kind": "mystery"}, lang="en", gym_name="Ant Work") is None
    assert messages.render(None, lang="en", gym_name="Ant Work") is None
