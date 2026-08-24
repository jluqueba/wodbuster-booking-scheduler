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


# --------------------------------------------------------------------------
# Single-day override branches (T-BDO-015, ADR-0012 Decision 4)
#
# These three are keyed on ``outcome_source``, not ``terminal_status``:
# a substitution reads as ``granted`` and a skip reads as ``skipped``,
# so the status alone cannot tell them from an ordinary run.
# --------------------------------------------------------------------------


def _fallback_payload(**overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "kind": "booking_result",
        "terminal_status": "granted",
        "outcome_source": "override_fallback",
        "outcome_id": 51,
        "class_type": "WOD",
        "target_slot": "2026-07-10T18:30+00:00",
        "requested_class": "Gymnastics",
        "requested_time": "19:00",
        "fallback_reason": "full",
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    ("lang", "reason"),
    [("en", "that class was full"), ("es", "esa clase estaba completa")],
)
def test_fallback_granted_names_booked_requested_and_reason(lang: str, reason: str) -> None:
    """FR-015, INV-008: a substitution is never silent."""
    body = messages.render(_fallback_payload(), lang=lang, gym_name="Ant Work")

    assert body is not None
    assert "[Ant Work]" in body
    assert "#51" in body
    assert "WOD" in body  # booked class
    assert "Gymnastics" in body  # requested class
    assert "19:00" in body  # requested time
    assert reason in body  # why the substitution happened


@pytest.mark.parametrize(
    ("lang", "override_reason", "rule_reason"),
    [
        ("en", "that class was full", "that class never appeared on the schedule"),
        ("es", "esa clase estaba completa", "esa clase no apareció en el horario"),
    ],
)
def test_fallback_exhausted_names_both_failures(
    lang: str, override_reason: str, rule_reason: str
) -> None:
    """FR-016, CC-007: nothing booked, and both failures are named."""
    body = messages.render(
        _fallback_payload(terminal_status="class_not_visible"),
        lang=lang,
        gym_name="Ant Work",
    )

    assert body is not None
    assert "Gymnastics" in body  # the override's own target
    assert "WOD" in body  # the rule's class
    assert override_reason in body
    assert rule_reason in body


@pytest.mark.parametrize(
    ("lang", "phrase"),
    [("en", "You marked this day"), ("es", "Marcaste este día")],
)
def test_override_skip_reads_as_a_decision_not_a_failure(lang: str, phrase: str) -> None:
    """FR-030: a skip must not read as something that went wrong."""
    body = messages.render(
        {
            "kind": "booking_result",
            "terminal_status": "skipped",
            "outcome_source": "override_skip",
            "outcome_id": 77,
            "class_type": "WOD",
            "target_slot": "2026-07-10T18:30+00:00",
        },
        lang=lang,
        gym_name="Ant Work",
    )

    assert body is not None
    assert "[Ant Work]" in body
    assert "#77" in body
    assert phrase in body
    # No failure vocabulary: the chain was never walked, so naming a
    # reason would invent a problem the user does not have.
    for reason in ("full", "completa", "horario", "schedule"):
        assert reason not in body


def test_fallback_branch_does_not_hijack_an_ordinary_granted_run() -> None:
    """``outcome_source`` defaults to ``rule``: the pre-feature path is untouched."""
    body = messages.render(
        {
            "kind": "booking_result",
            "terminal_status": "granted",
            "outcome_id": 42,
            "class_type": "WOD",
            "target_slot": "2026-07-10T18:30+00:00",
        },
        lang="en",
        gym_name="Ant Work",
    )
    assert body is not None
    assert "Substitution" not in body
