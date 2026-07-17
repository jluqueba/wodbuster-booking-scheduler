"""Unit tests for :class:`ManualBookingService` (US8.1, US8.T1).

Drives the one-off booking service with scripted fakes (no Postgres):

- A synchronous fake WodBuster client whose ``load_class`` /
  ``inscribir`` responses are scripted and whose calls are recorded.
- An in-memory cookie store and a ``MagicMock`` session factory.

The central guarantee (US8.T1 / CC-010) is that an out-of-window class
is rejected with :class:`BookingWindowClosedError` and *zero* booking
(``inscribir``) calls. The read-only ``LoadClass`` probe is allowed.

Real Postgres coverage of the granted path lives in the component
tests (``tests/component/test_manual_booking.py`` and the Telegram
webhook suite).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest

from wodbuster_worker.booking.manual import (
    BookingWindowClosedError,
    ClassNotVisibleError,
    ManualBookingResult,
    ManualBookingService,
    NoCookieError,
)
from wodbuster_worker.wodbuster_client.client import (
    BookingActionResponse,
    LoadClassResponse,
)

# ---------------------------------------------------------------------------
# Test doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeClient:
    """Scripted WodBuster client mirroring the executor unit fakes.

    ``load_class`` repeats the same payload on every call (the service
    reads once for the window probe, the delegated single-attempt reads
    once more to resolve the slot). ``inscribir`` is popped left to
    right; call counts are recorded so the window-closed test can assert
    *no* booking call was issued.
    """

    load_payload: dict[str, Any]
    inscribir_responses: list[Any] = field(default_factory=list)
    load_class_calls: list[dict[str, Any]] = field(default_factory=list)
    inscribir_calls: list[dict[str, Any]] = field(default_factory=list)

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        self.load_class_calls.append({"cookie": cookie_value, "ticks": ticks})
        return LoadClassResponse(status_code=200, latency_ms=10.0, payload=self.load_payload)

    def inscribir(
        self, cookie_value: str, *, class_id: str | int, ticks: int
    ) -> BookingActionResponse:
        self.inscribir_calls.append({"cookie": cookie_value, "class_id": class_id, "ticks": ticks})
        if not self.inscribir_responses:
            raise AssertionError("fake client: no scripted inscribir response remaining")
        value = self.inscribir_responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


class _FakeCookieStore:
    def __init__(self, cookie: str | None) -> None:
        self._cookie = cookie

    def load(self, session: Any, operator_id: int) -> str | None:
        return self._cookie


@contextmanager
def _session_ctx() -> Iterator[Any]:
    yield MagicMock()


def _load_payload(
    *,
    hora: str = "18:30:00",
    nombre: str = "WOD",
    slot_id: int = 45654,
    seconds_until_publication: float = -100.0,
) -> dict[str, Any]:
    """LoadClass payload wrapping one slot in the real nested shape."""
    slot = {
        "Id": slot_id,
        "Nombre": nombre,
        "HoraComienzo": hora,
        "TipoEstado": "Inscribible",
        "Plazas": 16,
        "AtletasEnListaDeEspera": 0,
    }
    return {
        "Data": [{"Hora": hora, "Valores": [{"Valor": slot}]}],
        "SegundosHastaPublicacion": seconds_until_publication,
    }


def _inscribir_granted() -> BookingActionResponse:
    return BookingActionResponse(
        status_code=200,
        latency_ms=25.0,
        outcome="granted",
        raw_res="Ok",
        payload={"Res": "Ok", "Data": []},
    )


def _service(
    *,
    client: _FakeClient,
    cookie: str | None = "cookie-abc",
) -> ManualBookingService:
    return ManualBookingService(
        client=client,  # type: ignore[arg-type]
        cookie_store=_FakeCookieStore(cookie),  # type: ignore[arg-type]
        session_factory=_session_ctx,
        operator_idu=None,
    )


_TARGET_DATE = date(2026, 7, 15)


# ---------------------------------------------------------------------------
# US8.T1 — window closed rejects with no booking call (CC-010)
# ---------------------------------------------------------------------------


def test_window_closed_rejects_without_booking() -> None:
    """A positive countdown rejects before any ``inscribir`` call."""
    client = _FakeClient(
        load_payload=_load_payload(seconds_until_publication=3600.0),
        inscribir_responses=[_inscribir_granted()],
    )
    service = _service(client=client)

    with pytest.raises(BookingWindowClosedError) as excinfo:
        service.book(operator_id=1, target_date=_TARGET_DATE, target_time="18:30")

    assert excinfo.value.seconds_until_open == 3600.0
    # The read-only probe is allowed; the mutating call must NOT fire.
    assert client.load_class_calls, "the window probe LoadClass read should happen"
    assert client.inscribir_calls == []


def test_no_cookie_rejects_without_any_upstream_call() -> None:
    """A missing cookie rejects before touching WodBuster at all."""
    client = _FakeClient(
        load_payload=_load_payload(),
        inscribir_responses=[_inscribir_granted()],
    )
    service = _service(client=client, cookie=None)

    with pytest.raises(NoCookieError):
        service.book(operator_id=1, target_date=_TARGET_DATE, target_time="18:30")

    assert client.load_class_calls == []
    assert client.inscribir_calls == []


def test_no_class_at_time_rejects_without_booking() -> None:
    """No slot at the requested time rejects before ``inscribir``."""
    client = _FakeClient(
        load_payload=_load_payload(hora="20:00:00"),
        inscribir_responses=[_inscribir_granted()],
    )
    service = _service(client=client)

    with pytest.raises(ClassNotVisibleError):
        service.book(operator_id=1, target_date=_TARGET_DATE, target_time="18:30")

    assert client.inscribir_calls == []


def test_invalid_time_raises_value_error() -> None:
    """A malformed time string is a ``ValueError`` before any I/O."""
    client = _FakeClient(load_payload=_load_payload())
    service = _service(client=client)

    with pytest.raises(ValueError):
        service.book(operator_id=1, target_date=_TARGET_DATE, target_time="notatime")

    assert client.load_class_calls == []


# ---------------------------------------------------------------------------
# Granted delegation (happy path)
# ---------------------------------------------------------------------------


def test_granted_delegates_and_returns_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """In-window class resolves its type and returns a granted result."""
    writer_calls: list[dict[str, Any]] = []

    def _fake_persist(session: Any, **kwargs: Any) -> Any:
        writer_calls.append(kwargs)
        stub = MagicMock()
        stub.id = 4242
        return stub

    monkeypatch.setattr("wodbuster_worker.booking.executor.persist_outcome", _fake_persist)

    client = _FakeClient(
        load_payload=_load_payload(nombre="WOD", hora="18:30:00"),
        inscribir_responses=[_inscribir_granted()],
    )
    service = _service(client=client)

    result = service.book(operator_id=1, target_date=_TARGET_DATE, target_time="18:30")

    assert isinstance(result, ManualBookingResult)
    assert result.terminal_status == "granted"
    assert result.outcome_id == 4242
    assert result.class_type == "WOD"
    assert len(client.inscribir_calls) == 1
    # US8: manual bookings carry no rule (rule_id IS NULL).
    assert writer_calls[-1]["rule_id"] is None
