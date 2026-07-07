"""Unit tests for :class:`CookieValidator` (US3.T1).

The validator is driven through a fake :class:`WodBusterClientProtocol`
implementation so the tests never touch the real gym subdomain. Each
test covers one branch of the classification decision tree:

- Successful probe → :class:`Valid` with a UTC timestamp.
- Server rejects cookie → :class:`Rejected` with a reason.
- Transport error → :class:`Unknown` (retry-appropriate verdict).
- Protocol error → :class:`Unknown` (server-degraded verdict).
- Empty input → :class:`Rejected` without hitting the network.

These map one-to-one to the four denial paths US3.6's ``POST /cookie``
route will render.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from wodbuster_worker.security.cookie import (
    CookieValidator,
    Rejected,
    Unknown,
    Valid,
)
from wodbuster_worker.wodbuster_client.client import (
    LoadClassResponse,
    WodBusterAuthError,
    WodBusterProtocolError,
    WodBusterTransportError,
)


class _FakeClient:
    """Minimal :class:`WodBusterClientProtocol` fake.

    Records the arguments of the most recent call so tests can assert
    the validator passed the cookie through unchanged, and lets each
    test dictate the outcome by setting ``result_or_exc`` before use.
    """

    def __init__(self, result_or_exc: object) -> None:
        self._result_or_exc = result_or_exc
        self.calls: list[tuple[str, int]] = []

    def load_class(self, cookie_value: str, ticks: int) -> LoadClassResponse:
        self.calls.append((cookie_value, ticks))
        if isinstance(self._result_or_exc, Exception):
            raise self._result_or_exc
        assert isinstance(self._result_or_exc, LoadClassResponse)
        return self._result_or_exc


def _ok_response() -> LoadClassResponse:
    return LoadClassResponse(status_code=200, latency_ms=42.0, payload={"Data": []})


def test_valid_cookie_returns_valid_with_utc_timestamp() -> None:
    client = _FakeClient(_ok_response())
    validator = CookieValidator(client)

    before = datetime.now(tz=UTC)
    result = validator.validate(".WBAuth-value-42")
    after = datetime.now(tz=UTC)

    assert isinstance(result, Valid)
    assert before <= result.probed_at <= after
    assert result.probed_at.tzinfo is UTC
    assert len(client.calls) == 1
    assert client.calls[0][0] == ".WBAuth-value-42"


def test_auth_error_maps_to_rejected() -> None:
    client = _FakeClient(WodBusterAuthError("redirected to login"))
    validator = CookieValidator(client)

    result = validator.validate("stale-cookie")

    assert isinstance(result, Rejected)
    assert "server rejected cookie" in result.reason
    # The upstream exception message is preserved so operators can
    # correlate with server logs if needed.
    assert "redirected to login" in result.reason


def test_transport_error_maps_to_unknown() -> None:
    client = _FakeClient(WodBusterTransportError("connection reset"))
    validator = CookieValidator(client)

    result = validator.validate("cookie-value")

    assert isinstance(result, Unknown)
    assert "could not reach WodBuster" in result.reason
    assert "connection reset" in result.reason


def test_protocol_error_maps_to_unknown() -> None:
    client = _FakeClient(WodBusterProtocolError("unexpected status 502"))
    validator = CookieValidator(client)

    result = validator.validate("cookie-value")

    assert isinstance(result, Unknown)
    assert "unexpected response" in result.reason
    assert "502" in result.reason


@pytest.mark.parametrize("empty", ["", "   ", "\t\n"])
def test_empty_input_is_rejected_without_probe(empty: str) -> None:
    client = _FakeClient(_ok_response())
    validator = CookieValidator(client)

    result = validator.validate(empty)

    assert isinstance(result, Rejected)
    assert "empty" in result.reason
    # Critical: no network call happens on empty input. That means we
    # never accidentally probe with a blank cookie, which the server
    # might treat as a soft-200 login page and confuse the classifier.
    assert client.calls == []


def test_validator_passes_a_utc_midnight_ticks_argument() -> None:
    client = _FakeClient(_ok_response())
    validator = CookieValidator(client)

    validator.validate("some-cookie")

    _, ticks = client.calls[0]
    # ticks is the unix timestamp of today at 00:00 UTC; ensure it
    # aligns to a midnight boundary rather than "now".
    ticks_dt = datetime.fromtimestamp(ticks, tz=UTC)
    assert ticks_dt.hour == 0
    assert ticks_dt.minute == 0
    assert ticks_dt.second == 0
