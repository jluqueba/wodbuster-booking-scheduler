"""Unit tests for the Healthchecks.io pinger (US2.5, US2.T5).

Uses ``httpx.MockTransport`` so no live network I/O. Verifies the
happy path, the "any 2xx counts" contract, and every failure mode
(non-2xx, timeout, transport error) returning ``False`` without
raising into the scheduler.
"""

from __future__ import annotations

import httpx

from wodbuster_worker.observability.healthchecks import ping


def _client_with_handler(
    handler,
) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_ping_posts_to_url_and_returns_true_on_200() -> None:
    captured: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["method"] = request.method
        captured["url"] = str(request.url)
        return httpx.Response(200)

    with _client_with_handler(handler) as client:
        result = ping("https://hc-ping.com/abcd-1234", client=client)

    assert result is True
    assert captured["method"] == "POST"
    assert captured["url"] == "https://hc-ping.com/abcd-1234"


def test_ping_treats_any_2xx_status_as_success() -> None:
    """Healthchecks.io returns 200 today but the contract is 2xx —
    204 or 202 must count too."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    with _client_with_handler(handler) as client:
        assert ping("https://hc-ping.com/x", client=client) is True


def test_ping_returns_false_on_non_2xx() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with _client_with_handler(handler) as client:
        assert ping("https://hc-ping.com/x", client=client) is False


def test_ping_returns_false_on_timeout() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("connect timeout", request=request)

    with _client_with_handler(handler) as client:
        assert ping("https://hc-ping.com/x", client=client) is False


def test_ping_returns_false_on_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("dns failure", request=request)

    with _client_with_handler(handler) as client:
        assert ping("https://hc-ping.com/x", client=client) is False
