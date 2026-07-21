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

import types as _types
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

# Capture the real ``_align_to_publication`` at import time — the
# autouse ``_skip_alignment`` fixture replaces it during every test,
# but the alignment tests need the real body. Rebinding via
# ``types.MethodType`` in each of those tests gives the instance
# back the unpatched implementation without disturbing the class.
_REAL_ALIGN_TO_PUBLICATION = BookingExecutor._align_to_publication


@pytest.fixture(autouse=True)
def _no_vacation_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the US7.2 skip guard to "no covering window".

    The guard opens a session against ``self._session_factory`` — in
    unit tests that factory yields a ``MagicMock`` whose attribute
    accesses return truthy sentinels. Without an explicit patch the
    guard would treat every rule as vacation-shielded and every test
    would terminate as ``skipped``. Tests that need to exercise the
    guard's positive branch override this fixture locally.
    """
    monkeypatch.setattr(
        "wodbuster_worker.booking.executor.find_covering_window",
        lambda session, *, operator_id, target_slot: None,
    )


@pytest.fixture(autouse=True)
def _skip_alignment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default the US1.5 countdown alignment to a no-op.

    Alignment issues an extra ``LoadClass`` call at the top of
    :meth:`BookingExecutor.book`. Existing tests script a precise
    number of responses for the primary + second-shot attempts;
    without this patch every scripted response would be off by one.
    Tests that exercise alignment explicitly clear the patch by
    replacing ``BookingExecutor._align_to_publication`` on the
    instance they build.
    """
    monkeypatch.setattr(
        "wodbuster_worker.booking.executor.BookingExecutor._align_to_publication",
        lambda self, *, cookie, ticks, rule_id: None,
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
        self.inscribir_calls.append({"cookie": cookie_value, "class_id": class_id, "ticks": ticks})
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
    operator_idu: str | None = None,
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
        monkeypatch.setattr("wodbuster_worker.booking.executor.persist_outcome", writer)

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
        operator_idu=operator_idu,
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
    """Build a LoadClass response wrapping ``slots`` in the real shape.

    Real payload nests instances under ``Data[i].Valores[j].Valor``.
    Callers pass a flat list of instance dicts; this helper groups
    them into buckets by ``HoraComienzo`` and wraps each under the
    ``Valor`` layer.
    """
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
    buckets: dict[str, list[dict[str, Any]]] = {}
    for slot in slots:
        hora = slot.get("HoraComienzo", "00:00:00")
        buckets.setdefault(hora, []).append({"Valor": slot})
    data = [{"Hora": hora, "Valores": valores} for hora, valores in buckets.items()]
    return LoadClassResponse(
        status_code=200,
        latency_ms=10.0,
        payload={"Data": data, "SegundosHastaPublicacion": -100.0},
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


# Operator identity used by the enrollment-based success tests. The
# ``idu`` travels dash-free; the athlete ``Url`` carries the dashed GUID.
_OP_IDU = "aae990e4fa584cfc894de204f0e37605"
_OP_GUID = "aae990e4-fa58-4cfc-894d-e204f0e37605"


def _inscribir_res_none() -> BookingActionResponse:
    """Mirror the production booking response: a 200 whose ``Res`` is a
    non-string (so the classifier yields ``unknown``) plus the full
    calendar body under ``Data``."""
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="unknown",
        raw_res=None,
        payload={"Res": True, "Data": []},
    )


def _load_class_enrolled(
    *,
    slot_id: int = 45654,
    hora: str = "21:30:00",
    nombre: str = "WOD",
    plazas: int = 16,
    enrolled: bool = True,
    occupied: int = 1,
) -> LoadClassResponse:
    """LoadClass payload whose slot ``slot_id`` carries an
    ``AtletasEntrenando`` list; the operator is present iff ``enrolled``.
    """
    athletes: list[dict[str, Any]] = []
    if enrolled:
        athletes.append(
            {"Id": 94, "DisplayName": "Luque", "Url": f"/athlete/athletes.aspx?gid={_OP_GUID}"}
        )
    while len(athletes) < occupied:
        i = len(athletes)
        athletes.append(
            {
                "Id": 1000 + i,
                "DisplayName": f"Other{i}",
                "Url": f"/athlete/athletes.aspx?gid=stranger-{i}",
            }
        )
    return _load_class_payload(
        slots=[
            {
                "Id": slot_id,
                "Nombre": nombre,
                "HoraComienzo": hora,
                "Plazas": plazas,
                "AtletasEntrenando": athletes,
            }
        ]
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


def test_vacation_skip_guard_short_circuits_before_wodbuster(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """US7.2: an open vacation window covering ``target_slot`` yields a
    ``skipped`` terminal and no upstream call."""
    ex, client, writer = _executor(monkeypatch=monkeypatch)

    fake_window = MagicMock()
    fake_window.id = 77
    monkeypatch.setattr(
        "wodbuster_worker.booking.executor.find_covering_window",
        lambda session, *, operator_id, target_slot: fake_window,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "skipped"
    assert client.load_class_calls == []
    assert client.inscribir_calls == []
    assert writer.calls[0]["terminal_status"] == "skipped"
    assert writer.calls[0]["response_payload"] == "vacation window #77"


def test_vacation_skip_guard_absent_lets_booking_proceed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No covering window → booking flow continues normally."""
    ex, client, writer = _executor(
        load_class_responses=[_load_class_payload()],
        inscribir_responses=[_inscribir_ok()],
        monkeypatch=monkeypatch,
    )
    monkeypatch.setattr(
        "wodbuster_worker.booking.executor.find_covering_window",
        lambda session, *, operator_id, target_slot: None,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    assert len(client.inscribir_calls) == 1
    assert writer.calls[0]["terminal_status"] == "granted"


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
# Enrollment-based success detection (Res / TipoEstado proved unreliable)
# ---------------------------------------------------------------------------


def test_res_none_but_operator_enrolled_persists_granted(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production regression: the booking response carries a non-string
    ``Res`` (classifier -> ``unknown``) yet the confirming read shows the
    operator enrolled. The old code reported ``upstream_unavailable``;
    the fix must persist ``granted``."""
    ex, client, writer = _executor(
        load_class_responses=[
            _load_class_payload(),  # primary find
            _load_class_enrolled(enrolled=True),  # confirming read
        ],
        inscribir_responses=[_inscribir_res_none()],
        operator_idu=_OP_IDU,
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    assert result.fallback_index == 0
    call = writer.calls[0]
    assert call["terminal_status"] == "granted"
    assert "enrolled=confirmed" in call["response_payload"]
    # inscribir response had no Data, so a confirming LoadClass read fired.
    assert len(client.load_class_calls) == 2


def test_inscribir_response_already_shows_enrollment_skips_confirming_read(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the mutating response already lists the operator, no extra
    confirming LoadClass read is issued."""
    enrolled_payload = _load_class_enrolled(enrolled=True).payload
    inscribir = BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="unknown",
        raw_res=None,
        payload=enrolled_payload,
    )
    ex, client, _writer = _executor(
        load_class_responses=[_load_class_payload()],  # only the primary find
        inscribir_responses=[inscribir],
        operator_idu=_OP_IDU,
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    # Only the primary-find call; the inscribir response was conclusive.
    assert len(client.load_class_calls) == 1


def test_not_enrolled_and_slot_full_persists_full(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, _client, writer = _executor(
        load_class_responses=[
            _load_class_payload(),
            _load_class_enrolled(enrolled=False, occupied=16, plazas=16),  # full
        ],
        inscribir_responses=[_inscribir_res_none()],
        operator_idu=_OP_IDU,
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "full"
    assert "was full" in writer.calls[0]["telegram_text"]


def test_not_enrolled_and_not_full_persists_upstream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ex, _client, _writer = _executor(
        load_class_responses=[
            _load_class_payload(),
            _load_class_enrolled(enrolled=False, occupied=3, plazas=16),  # room left
        ],
        inscribir_responses=[_inscribir_res_none()],
        operator_idu=_OP_IDU,
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "upstream_unavailable"


def test_confirming_read_failure_falls_back_to_res_classifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the confirming LoadClass read raises, classification falls back
    to the Res outcome rather than crashing."""
    ex, _client, _writer = _executor(
        load_class_responses=[
            _load_class_payload(),  # primary find
            WodBusterTransportError("boom"),  # confirming read fails
        ],
        inscribir_responses=[_inscribir_ok()],  # recognised granted Res
        operator_idu=_OP_IDU,
        monkeypatch=monkeypatch,
    )

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"


def test_enrolled_takes_priority_over_full_second_shot_not_walked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A confirmed enrollment is terminal even when a second shot is
    configured — the walk only happens on a full primary."""
    ex, client, _writer = _executor(
        load_class_responses=[
            _load_class_payload(),
            _load_class_enrolled(enrolled=True),
        ],
        inscribir_responses=[_inscribir_res_none()],
        operator_idu=_OP_IDU,
        monkeypatch=monkeypatch,
    )

    result = ex.book(
        rule=_rule(second_shot_class_type="Halterofilia", second_shot_class_time="20:30"),
        target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC),
    )

    assert result.terminal_status == "granted"
    assert result.fallback_index == 0
    assert len(client.inscribir_calls) == 1


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


# ---------------------------------------------------------------------------
# US1.5 countdown alignment
# ---------------------------------------------------------------------------


def _aligned_payload(seconds_until: float) -> LoadClassResponse:
    """LoadClass response scripted with a specific countdown value."""
    base = _load_class_payload()
    return LoadClassResponse(
        status_code=base.status_code,
        latency_ms=base.latency_ms,
        payload={**base.payload, "SegundosHastaPublicacion": seconds_until},
    )


def test_alignment_polls_until_countdown_below_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Executor polls LoadClass while ``SegundosHastaPublicacion`` is
    above the threshold, then hands off to the primary attempt."""
    sleeps: list[float] = []
    ex, client, writer = _executor(
        load_class_responses=[
            _aligned_payload(15.0),
            _aligned_payload(8.0),
            _aligned_payload(0.1),
            _load_class_payload(),  # primary attempt's own LoadClass
        ],
        inscribir_responses=[_inscribir_ok()],
        monkeypatch=monkeypatch,
    )
    ex._align_to_publication = _types.MethodType(_REAL_ALIGN_TO_PUBLICATION, ex)  # type: ignore[method-assign]
    ex._sleep = sleeps.append  # type: ignore[method-assign]

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    # Three alignment polls (15s -> 8s -> 0.1s aligned) + one primary
    # LoadClass = four total.
    assert len(client.load_class_calls) == 4
    # Two sleeps between the three alignment polls; no sleep after
    # the aligned poll.
    assert len(sleeps) == 2
    assert writer.calls[0]["terminal_status"] == "granted"


def test_alignment_absent_countdown_falls_through_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Missing ``SegundosHastaPublicacion`` skips alignment, so the
    booking flow keeps working against servers that do not surface
    the countdown."""
    sleeps: list[float] = []
    # Default ``_load_class_payload`` uses ``-100.0`` for the field,
    # which is below the threshold — alignment exits on the first
    # poll. Verifies the "aligned on first check" branch.
    ex, client, _writer = _executor(
        load_class_responses=[_load_class_payload(), _load_class_payload()],
        inscribir_responses=[_inscribir_ok()],
        monkeypatch=monkeypatch,
    )
    ex._align_to_publication = _types.MethodType(_REAL_ALIGN_TO_PUBLICATION, ex)  # type: ignore[method-assign]
    ex._sleep = sleeps.append  # type: ignore[method-assign]

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    assert len(client.load_class_calls) == 2  # 1 alignment + 1 primary
    assert sleeps == []


def test_alignment_deadline_bail_still_runs_booking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A publication clock stuck above the threshold does not block
    the executor: alignment gives up after ``alignment_deadline_s``
    and hands off to the primary attempt anyway."""
    time_series: list[float] = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 10.0]
    sleeps: list[float] = []
    ex, client, writer = _executor(
        load_class_responses=[
            _aligned_payload(30.0),
            _aligned_payload(30.0),
            _aligned_payload(30.0),
            _load_class_payload(),  # primary attempt's LoadClass
        ],
        inscribir_responses=[_inscribir_ok()],
        monkeypatch=monkeypatch,
        time_series=time_series,
    )
    ex._align_to_publication = _types.MethodType(_REAL_ALIGN_TO_PUBLICATION, ex)  # type: ignore[method-assign]
    ex._alignment_deadline_s = 4.0  # type: ignore[attr-defined]
    ex._sleep = sleeps.append  # type: ignore[method-assign]

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    # Booking still fires; deadline is a safety net, not a failure.
    assert writer.calls[0]["terminal_status"] == "granted"
    # At least one alignment poll happened before the deadline hit.
    assert len(client.load_class_calls) >= 2


def test_alignment_upstream_error_swallowed_and_booking_proceeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A transient upstream error during alignment lets the primary
    attempt run — that path already surfaces terminal statuses."""
    ex, client, writer = _executor(
        load_class_responses=[
            WodBusterTransportError("connection reset"),
            _load_class_payload(),  # primary attempt succeeds
        ],
        inscribir_responses=[_inscribir_ok()],
        monkeypatch=monkeypatch,
    )
    ex._align_to_publication = _types.MethodType(_REAL_ALIGN_TO_PUBLICATION, ex)  # type: ignore[method-assign]

    result = ex.book(rule=_rule(), target_slot=datetime(2026, 7, 15, 21, 30, tzinfo=UTC))

    assert result.terminal_status == "granted"
    assert len(client.load_class_calls) == 2
    assert writer.calls[0]["terminal_status"] == "granted"
