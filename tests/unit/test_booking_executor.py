"""Unit tests for :class:`BookingExecutor` (US1.3, US1.6, US1.7).

Drives the state machine end-to-end with:

- A synchronous fake WodBuster client whose responses are scripted.
- An in-memory session factory that captures ``persist_outcome``
  invocations without a real DB.

That combination lets each test focus on one state transition —
primary granted, primary full + second shot granted, cookie invalid,
class not visible after retry, unknown Res escalation, etc.

Real Postgres coverage of the writer lives in
``tests/component/test_booking_outcomes.py``.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from unittest.mock import MagicMock

import pytest

from wodbuster_worker.booking.executor import BookingExecutor
from wodbuster_worker.persistence.models import SchedulerRule
from wodbuster_worker.wodbuster_client.client import (
    BookingActionResponse,
    LoadClassResponse,
    WodBusterAuthError,
    WodBusterTransportError,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeClient:
    """Scripted WodBuster client.

    ``load_class_responses`` is popped left-to-right on each call.
    A remaining ``LoadClassResponse`` is returned; an exception is
    raised. When exhausted the last value is repeated so a long
    retry loop does not blow up unless the test intends to.

    ``inscribir_responses`` follows the same rule.
    """

    load_class_responses: list[Any] = field(default_factory=list)
    inscribir_responses: list[Any] = field(default_factory=list)
    load_class_calls: list[dict[str, Any]] = field(default_factory=list)
    inscribir_calls: list[dict[str, Any]] = field(default_factory=list)

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        self.load_class_calls.append({"cookie": cookie_value, "ticks": ticks})
        return self._pop(self.load_class_responses)

    def inscribir(
        self, cookie_value: str, *, class_id: str | int, ticks: int
    ) -> BookingActionResponse:
        self.inscribir_calls.append(
            {"cookie": cookie_value, "class_id": class_id, "ticks": ticks}
        )
        return self._pop(self.inscribir_responses)

    @staticmethod
    def _pop(script: list[Any]) -> Any:
        if not script:
            raise AssertionError("fake client: no scripted response remaining")
        value = script[0] if len(script) == 1 else script.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _RecordingWriter:
    """Captures :func:`persist_outcome` invocations from the executor.

    We monkeypatch the writer symbol on the executor module so the
    state machine's own return value (a stub outcome) drives
    :class:`BookingResult` construction without a real Postgres.
    """

    def __init__(self, next_outcome_id: int = 100) -> None:
        self._next_id = next_outcome_id
        self.calls: list[dict[str, Any]] = []

    def __call__(self, session: Any, **kwargs: Any) -> Any:
        self._next_id += 1
        self.calls.append({"session": session, **kwargs})
        stub = MagicMock()
        stub.id = self._next_id
        return stub


class _FakeCookieStore:
    def __init__(self, cookie: str | None) -> None:
        self._cookie = cookie

    def load(self, session: Any, operator_id: int) -> str | None:
        return self._cookie


@contextmanager
def _session_ctx() -> Iterator[Any]:
    """Session factory that yields a MagicMock supporting ``.commit()``."""
    session = MagicMock()
    yield session


def _executor(
    *,
    load_class_responses: list[Any] | None = None,
    inscribir_responses: list[Any] | None = None,
    cookie: str | None = "cookie-abc",
    writer: _RecordingWriter | None = None,
    retry_interval_s: float = 0.0,
    retry_timeout_s: float = 0.05,
    monkeypatch: pytest.MonkeyPatch | None = None,
    time_series: list[float] | None = None,
) -> tuple[BookingExecutor, _FakeClient, _RecordingWriter]:
    client = _FakeClient(
        load_class_responses=load_class_responses or [],
        inscribir_responses=inscribir_responses or [],
    )
    writer = writer or _RecordingWriter()
    if monkeypatch is not None:
        monkeypatch.setattr(
            "wodbuster_worker.booking.executor.persist_outcome", writer
        )

    time_iter = iter(time_series or [])
    # Once the scripted series is exhausted, advance in large steps so
    # a retry loop that expects wall-clock progress terminates instead
    # of spinning forever.
    fallback_counter = [1000.0]

    def _time() -> float:
        try:
            return next(time_iter)
        except StopIteration:
            fallback_counter[0] += 1000.0
            return fallback_counter[0]

    ex = BookingExecutor(
        client=client,
        session_factory=_session_ctx,
        cookie_store=_FakeCookieStore(cookie),  # type: ignore[arg-type]
        retry_interval_s=retry_interval_s,
        retry_timeout_s=retry_timeout_s,
        sleep=lambda _s: None,
        time_source=_time if time_series else lambda: 0.0,
    )
    return ex, client, writer


def _rule(
    *,
    class_type: str = "WOD",
    class_time: str = "21:30",
    second_shot_class_type: str | None = None,
    second_shot_class_time: str | None = None,
) -> SchedulerRule:
    """Build a rule without touching Postgres (uses SQLAlchemy transient state)."""
    rule = SchedulerRule(
        operator_id=1,
        day_of_week=2,
        class_type=class_type,
        class_time=class_time,
        booking_opens_days_before=2,
        booking_opens_at="21:30",
        second_shot_class_type=second_shot_class_type,
        second_shot_class_time=second_shot_class_time,
        active=True,
    )
    # Assign a synthetic id so BookingOutcome rows can reference it.
    rule.id = 42
    return rule


def _load_class_payload(
    *,
    slots: list[dict[str, Any]] | None = None,
) -> LoadClassResponse:
    if slots is None:
        slots = [
            {
                "Id": 45654,
                "Nombre": "WOD",
                "HoraComienzo": "21:30:00",
                "TipoEstado": "Inscribible",
                "Plazas": 16,
                "AtletasEnListaDeEspera": 0,
            }
        ]
    return LoadClassResponse(
        status_code=200,
        latency_ms=10.0,
        payload={"Data": slots, "SegundosHastaPublicacion": -100.0},
    )


def _inscribir_ok(res: str = "Ok") -> BookingActionResponse:
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="granted",
        raw_res=res,
        payload={"Res": res, "Data": []},
    )


def _inscribir_full() -> BookingActionResponse:
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="full",
        raw_res="Completa",
        payload={"Res": "Completa", "Data": []},
    )


def _inscribir_cookie_invalid() -> BookingActionResponse:
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="cookie_invalid",
        raw_res="SinAcceso",
        payload={"Res": "SinAcceso"},
    )


def _inscribir_unknown() -> BookingActionResponse:
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="unknown",
        raw_res="Weirdness",
        payload={"Res": "Weirdness"},
    )


# ---------------------------------------------------------------------------
# Happy-path tests
# ---------------------------------------------------------------------------


def test_primary_slot_granted_persists_granted_with_index_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, client, writer = _executor(
        load_class_responses=[_load_class_payload()],
        inscribir_responses=[_inscribir_ok()],
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    assert result.fallback_index == 0
    # Exactly one inscribir call (no fallback walk).
    assert len(client.inscribir_calls) == 1
    assert client.inscribir_calls[0]["class_id"] == 45654
    # Writer received the granted terminal.
    call = writer.calls[0]
    assert call["terminal_status"] == "granted"
    assert call["granted_fallback_index"] == 0
    assert "Booked WOD" in call["telegram_text"]


def test_primary_full_walks_to_second_shot_and_grants(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, client, writer = _executor(
        load_class_responses=[
            # Primary find: WOD present.
            _load_class_payload(),
            # Second-shot find: different class type at 20:30.
            _load_class_payload(
                slots=[
                    {
                        "Id": 88888,
                        "Nombre": "Halterofilia",
                        "HoraComienzo": "20:30:00",
                        "TipoEstado": "Inscribible",
                    }
                ]
            ),
        ],
        inscribir_responses=[_inscribir_full(), _inscribir_ok()],
        monkeypatch=monkeypatch,
    )

    result = ex.book(
        rule=_rule(
            second_shot_class_type="Halterofilia",
            second_shot_class_time="20:30",
        ),
        target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
    )

    assert result.terminal_status == "granted"
    assert result.fallback_index == 1
    assert [c["class_id"] for c in client.inscribir_calls] == [45654, 88888]
    call = writer.calls[0]
    assert call["target_class"] == "Halterofilia"
    assert call["granted_fallback_index"] == 1
    assert "second shot" in call["telegram_text"]


def test_primary_full_no_second_shot_persists_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, _client, writer = _executor(
        load_class_responses=[_load_class_payload()],
        inscribir_responses=[_inscribir_full()],
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "full"
    assert result.fallback_index is None
    assert writer.calls[0]["terminal_status"] == "full"
    assert "was full" in writer.calls[0]["telegram_text"]


def test_primary_full_second_shot_also_full_persists_full_for_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, _client, writer = _executor(
        load_class_responses=[
            _load_class_payload(),
            _load_class_payload(
                slots=[
                    {
                        "Id": 88888,
                        "Nombre": "Halterofilia",
                        "HoraComienzo": "20:30:00",
                        "TipoEstado": "Inscribible",
                    }
                ]
            ),
        ],
        inscribir_responses=[_inscribir_full(), _inscribir_full()],
        monkeypatch=monkeypatch,
    )

    result = ex.book(
        rule=_rule(
            second_shot_class_type="Halterofilia",
            second_shot_class_time="20:30",
        ),
        target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
    )

    assert result.terminal_status == "full"
    call = writer.calls[0]
    assert call["target_class"] == "Halterofilia"


# ---------------------------------------------------------------------------
# Failure-mode tests
# ---------------------------------------------------------------------------


def test_missing_cookie_short_circuits_to_cookie_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, client, writer = _executor(
        cookie=None,
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "cookie_invalid"
    # No HTTP call was ever made.
    assert client.load_class_calls == []
    assert client.inscribir_calls == []
    assert writer.calls[0]["response_payload"] == "no cookie on file"


def test_inscribir_cookie_invalid_res_marks_terminal_cookie_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, _client, writer = _executor(
        load_class_responses=[_load_class_payload()],
        inscribir_responses=[_inscribir_cookie_invalid()],
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "cookie_invalid"
    assert writer.calls[0]["target_class"] == "WOD"


def test_inscribir_auth_error_maps_to_cookie_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, _client, writer = _executor(
        load_class_responses=[_load_class_payload()],
        inscribir_responses=[WodBusterAuthError("redirected to login")],
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "cookie_invalid"
    assert "auth error" in writer.calls[0]["response_payload"]


def test_inscribir_transport_error_maps_to_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, _client, _writer = _executor(
        load_class_responses=[_load_class_payload()],
        inscribir_responses=[WodBusterTransportError("timeout")],
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "upstream_unavailable"


def test_inscribir_unknown_res_escalates_to_upstream_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, _client, writer = _executor(
        load_class_responses=[_load_class_payload()],
        inscribir_responses=[_inscribir_unknown()],
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "upstream_unavailable"
    # The raw Res string is preserved on the response_payload so the
    # classifier table can be extended after post-mortem.
    assert "Weirdness" in writer.calls[0]["response_payload"]


# ---------------------------------------------------------------------------
# Class-not-visible retry policy (US1.7)
# ---------------------------------------------------------------------------


def test_class_not_visible_retries_until_deadline_then_marks_terminal(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # LoadClass keeps returning an empty Data list.
    empty = _load_class_payload(slots=[])
    ex, client, _writer = _executor(
        load_class_responses=[empty],  # single value, repeated
        # No inscribir needed — the executor never reaches it.
        cookie="cookie-abc",
        monkeypatch=monkeypatch,
        # 5s interval, 20s budget → four attempts and out.
        retry_interval_s=5.0,
        retry_timeout_s=20.0,
        time_series=[0.0, 5.0, 5.0, 10.0, 10.0, 15.0, 15.0, 20.0, 20.0, 25.0],
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "class_not_visible"
    # Bounded number of load_class calls (the loop must terminate).
    assert 2 <= len(client.load_class_calls) <= 10
    assert client.inscribir_calls == []


def test_class_becomes_visible_on_retry_and_gets_booked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    empty = _load_class_payload(slots=[])
    good = _load_class_payload()
    ex, client, _writer = _executor(
        load_class_responses=[empty, good],
        inscribir_responses=[_inscribir_ok()],
        monkeypatch=monkeypatch,
        retry_interval_s=1.0,
        retry_timeout_s=10.0,
        time_series=[0.0, 1.0, 1.0, 2.0, 2.0, 3.0],
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    # Two LoadClass calls: first empty, second returned the slot.
    assert len(client.load_class_calls) == 2
    assert len(client.inscribir_calls) == 1


def test_transport_error_during_find_retries_then_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    good = _load_class_payload()
    ex, client, _writer = _executor(
        load_class_responses=[WodBusterTransportError("blip"), good],
        inscribir_responses=[_inscribir_ok()],
        monkeypatch=monkeypatch,
        retry_interval_s=1.0,
        retry_timeout_s=10.0,
        time_series=[0.0, 1.0, 1.0, 2.0, 2.0, 3.0],
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    assert len(client.load_class_calls) == 2


def test_second_shot_not_visible_terminates_after_retry(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = _load_class_payload()
    empty = _load_class_payload(slots=[])
    ex, _client, writer = _executor(
        load_class_responses=[primary, empty],
        inscribir_responses=[_inscribir_full()],
        monkeypatch=monkeypatch,
        retry_interval_s=1.0,
        retry_timeout_s=2.0,
        time_series=[0.0, 1.0, 1.0, 2.0, 2.0, 3.0],
    )

    result = ex.book(
        rule=_rule(
            second_shot_class_type="Halterofilia",
            second_shot_class_time="20:30",
        ),
        target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
    )

    assert result.terminal_status == "class_not_visible"
    assert writer.calls[0]["target_class"] == "Halterofilia"


# ---------------------------------------------------------------------------
# Guardrails
# ---------------------------------------------------------------------------


def test_naive_target_slot_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, _client, _writer = _executor(monkeypatch=monkeypatch)

    with pytest.raises(ValueError, match="timezone-aware"):
        ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30))


def test_ticks_derived_from_utc_midnight_of_target_slot(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, client, _writer = _executor(
        load_class_responses=[_load_class_payload()],
        inscribir_responses=[_inscribir_ok()],
        monkeypatch=monkeypatch,
    )

    # 2026-07-15 21:30 UTC → midnight UTC = 2026-07-15 00:00 UTC.
    expected_ticks = int(datetime(2026, 7, 15, tzinfo=UTC).timestamp())
    ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert client.load_class_calls[0]["ticks"] == expected_ticks
    assert client.inscribir_calls[0]["ticks"] == expected_ticks
