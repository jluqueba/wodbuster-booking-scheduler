"""Unit tests for :class:`WodBusterClient` (US1.1 slice — validator path).

Drives the client through :class:`httpx.MockTransport` so no live
network calls occur. Covers the three verdict-relevant branches the
validator distinguishes (success, auth failure, transport failure) plus
the two protocol edges that Phase 0 documented (redirect to login,
soft-200 HTML login page). The remaining branches (401/403, non-JSON
body, non-dict JSON) round out the classification surface.
"""

from __future__ import annotations

import json

import httpx
import pytest

from wodbuster_worker.wodbuster_client.client import (
    LoadClassResponse,
    WodBusterAuthError,
    WodBusterClient,
    WodBusterProtocolError,
    WodBusterTransportError,
)


def _client(handler: httpx.MockTransport) -> WodBusterClient:
    """Build a client wired to ``handler`` rather than the network."""
    http_client = httpx.Client(transport=handler, follow_redirects=False)
    return WodBusterClient(gym="testgym", idu="idu-abc", http_client=http_client)


def test_successful_response_returns_parsed_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Verify the client is calling the gym-scoped subdomain path
        # discovered in Phase 0.
        assert request.url.host == "testgym.wodbuster.com"
        assert request.url.path == "/athlete/handlers/LoadClass.ashx"
        # Cookie and query params are the two things a wrong client
        # implementation would silently drop; assert both.
        assert request.headers["cookie"].startswith(".WBAuth=")
        assert request.url.params["idu"] == "idu-abc"
        assert request.url.params["ticks"] == "1234567890"
        return httpx.Response(
            200,
            headers={"content-type": "text/json; charset=utf-8"},
            content=json.dumps({"Data": [], "TieneFiltros": True}).encode(),
        )

    result = _client(httpx.MockTransport(handler)).load_class(
        cookie_value="valid-cookie", ticks=1234567890
    )

    assert isinstance(result, LoadClassResponse)
    assert result.status_code == 200
    assert result.payload["TieneFiltros"] is True
    assert result.latency_ms >= 0.0


def test_redirect_to_login_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302,
            headers={"location": "/account/login.aspx?ReturnUrl=%2f"},
        )

    with pytest.raises(WodBusterAuthError, match="redirected to login"):
        _client(httpx.MockTransport(handler)).load_class(
            cookie_value="stale-cookie", ticks=1
        )


def test_unexpected_redirect_raises_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A redirect to something other than the login flow signals
        # server-side degradation; surface it as a protocol issue so
        # the validator classifies it as Unknown rather than Rejected.
        return httpx.Response(302, headers={"location": "/maintenance"})

    with pytest.raises(WodBusterProtocolError, match="unexpected redirect"):
        _client(httpx.MockTransport(handler)).load_class(
            cookie_value="any", ticks=1
        )


@pytest.mark.parametrize("status", [401, 403])
def test_401_and_403_raise_auth_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(WodBusterAuthError, match=str(status)):
        _client(httpx.MockTransport(handler)).load_class(
            cookie_value="any", ticks=1
        )


def test_non_2xx_non_auth_raises_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(WodBusterProtocolError, match="500"):
        _client(httpx.MockTransport(handler)).load_class(
            cookie_value="any", ticks=1
        )


def test_html_soft_200_raises_auth_error() -> None:
    # Phase 0 documented that WodBuster sometimes serves the login page
    # as HTML with a 200 status when the cookie is missing/invalid.
    # We detect that shape by content-type rather than parsing HTML.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=b"<html><body>login</body></html>",
        )

    with pytest.raises(WodBusterAuthError, match="content-type"):
        _client(httpx.MockTransport(handler)).load_class(
            cookie_value="any", ticks=1
        )


def test_invalid_json_body_raises_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"not json at all",
        )

    with pytest.raises(WodBusterProtocolError, match="invalid JSON"):
        _client(httpx.MockTransport(handler)).load_class(
            cookie_value="any", ticks=1
        )


def test_json_array_body_raises_protocol_error() -> None:
    # Phase 0 evidence is that LoadClass returns an object; a list at
    # the top level means the API shape changed and we shouldn't
    # silently claim success.
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"[]",
        )

    with pytest.raises(WodBusterProtocolError, match="expected JSON object"):
        _client(httpx.MockTransport(handler)).load_class(
            cookie_value="any", ticks=1
        )


def test_transport_error_wraps_httpx_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    with pytest.raises(WodBusterTransportError, match="DNS"):
        _client(httpx.MockTransport(handler)).load_class(
            cookie_value="any", ticks=1
        )


def test_constructor_rejects_empty_gym_and_idu() -> None:
    with pytest.raises(ValueError, match="gym"):
        WodBusterClient(gym="", idu="idu")
    with pytest.raises(ValueError, match="idu"):
        WodBusterClient(gym="g", idu="")
