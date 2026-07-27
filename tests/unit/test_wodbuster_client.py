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
    BookingActionResponse,
    LoadClassResponse,
    WodBusterAuthError,
    WodBusterClient,
    WodBusterProtocolError,
    WodBusterTransportError,
    classify_res,
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
        _client(httpx.MockTransport(handler)).load_class(cookie_value="stale-cookie", ticks=1)


def test_unexpected_redirect_raises_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # A redirect to something other than the login flow signals
        # server-side degradation; surface it as a protocol issue so
        # the validator classifies it as Unknown rather than Rejected.
        return httpx.Response(302, headers={"location": "/maintenance"})

    with pytest.raises(WodBusterProtocolError, match="unexpected redirect"):
        _client(httpx.MockTransport(handler)).load_class(cookie_value="any", ticks=1)


@pytest.mark.parametrize("status", [401, 403])
def test_401_and_403_raise_auth_error(status: int) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status)

    with pytest.raises(WodBusterAuthError, match=str(status)):
        _client(httpx.MockTransport(handler)).load_class(cookie_value="any", ticks=1)


def test_non_2xx_non_auth_raises_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    with pytest.raises(WodBusterProtocolError, match="500"):
        _client(httpx.MockTransport(handler)).load_class(cookie_value="any", ticks=1)


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
        _client(httpx.MockTransport(handler)).load_class(cookie_value="any", ticks=1)


def test_invalid_json_body_raises_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "application/json"},
            content=b"not json at all",
        )

    with pytest.raises(WodBusterProtocolError, match="invalid JSON"):
        _client(httpx.MockTransport(handler)).load_class(cookie_value="any", ticks=1)


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
        _client(httpx.MockTransport(handler)).load_class(cookie_value="any", ticks=1)


def test_transport_error_wraps_httpx_exception() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("simulated DNS failure")

    with pytest.raises(WodBusterTransportError, match="DNS"):
        _client(httpx.MockTransport(handler)).load_class(cookie_value="any", ticks=1)


def test_constructor_rejects_empty_gym_but_allows_absent_idu() -> None:
    with pytest.raises(ValueError, match="gym"):
        WodBusterClient(gym="", idu="idu")
    # ``idu`` is optional: an absent or empty idu yields a discovery-only
    # client (used by the add-gym flow before the gym's idu is known).
    WodBusterClient(gym="g")
    WodBusterClient(gym="g", idu="")


# ---------------------------------------------------------------------------
# classify_res — response classifier (US1.2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("Ok", "granted"),
        ("OK", "granted"),
        ("Correcto", "granted"),
        ("Reservada", "granted"),
        ("Inscrita", "granted"),
        ("Completa", "full"),
        ("Llena", "full"),
        ("SinPlazas", "full"),
        ("sin_plazas", "full"),
        ("SinAcceso", "cookie_invalid"),
        ("sin_sesion", "cookie_invalid"),
    ],
)
def test_classify_res_maps_known_values(raw: str, expected: str) -> None:
    assert classify_res(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", None, "SomethingWeIveNeverSeen", "12345"],
)
def test_classify_res_falls_back_to_unknown(raw: str | None) -> None:
    assert classify_res(raw) == "unknown"


def test_classify_res_is_case_and_whitespace_insensitive() -> None:
    assert classify_res("  ok  ") == "granted"
    assert classify_res("SIN ACCESO") == "cookie_invalid"


# ---------------------------------------------------------------------------
# inscribir / borrar — booking action methods (US1.1 completion)
# ---------------------------------------------------------------------------


def _booking_ok_response(res: str = "Ok") -> httpx.Response:
    return httpx.Response(
        200,
        headers={"content-type": "text/json; charset=utf-8"},
        content=json.dumps({"Res": res, "Data": [], "TieneFiltros": True}).encode(),
    )


def test_inscribir_success_returns_granted_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        # Correct endpoint and gym-scoped URL.
        assert request.url.host == "testgym.wodbuster.com"
        assert request.url.path == "/athlete/handlers/Calendario_Inscribir.ashx"
        # Client passes all three required query params.
        assert request.url.params["id"] == "45654"
        assert request.url.params["ticks"] == "1700000000"
        assert request.url.params["idu"] == "idu-abc"
        # Cookie header carries the .WBAuth value.
        assert request.headers["cookie"] == ".WBAuth=live-cookie"
        # Cache-buster present (any int).
        assert request.url.params["_"]
        return _booking_ok_response("Ok")

    result = _client(httpx.MockTransport(handler)).inscribir(
        cookie_value="live-cookie", class_id="45654", ticks=1700000000
    )

    assert isinstance(result, BookingActionResponse)
    assert result.outcome == "granted"
    assert result.raw_res == "Ok"
    assert result.status_code == 200
    assert result.payload["TieneFiltros"] is True


def test_inscribir_full_slot_returns_full_outcome() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _booking_ok_response("Completa")

    result = _client(httpx.MockTransport(handler)).inscribir(cookie_value="c", class_id=1, ticks=1)
    assert result.outcome == "full"
    assert result.raw_res == "Completa"


def test_inscribir_cookie_invalid_mid_flight_returns_cookie_invalid() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _booking_ok_response("SinAcceso")

    result = _client(httpx.MockTransport(handler)).inscribir(cookie_value="c", class_id=1, ticks=1)
    assert result.outcome == "cookie_invalid"


def test_inscribir_unknown_res_preserves_raw_and_classifies_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return _booking_ok_response("SomethingNewFromWodBuster")

    result = _client(httpx.MockTransport(handler)).inscribir(cookie_value="c", class_id=1, ticks=1)
    assert result.outcome == "unknown"
    # Raw value preserved so the executor can log / persist for
    # post-mortem — the classifier extension lives on this string.
    assert result.raw_res == "SomethingNewFromWodBuster"


def test_inscribir_missing_res_field_returns_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/json"},
            content=json.dumps({"Data": []}).encode(),
        )

    result = _client(httpx.MockTransport(handler)).inscribir(cookie_value="c", class_id=1, ticks=1)
    assert result.outcome == "unknown"
    assert result.raw_res is None


def test_inscribir_redirect_to_login_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/account/login.aspx?ReturnUrl=%2f"})

    with pytest.raises(WodBusterAuthError):
        _client(httpx.MockTransport(handler)).inscribir(cookie_value="c", class_id=1, ticks=1)


def test_inscribir_timeout_raises_transport_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow")

    with pytest.raises(WodBusterTransportError):
        _client(httpx.MockTransport(handler)).inscribir(cookie_value="c", class_id=1, ticks=1)


def test_inscribir_5xx_raises_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    with pytest.raises(WodBusterProtocolError, match="503"):
        _client(httpx.MockTransport(handler)).inscribir(cookie_value="c", class_id=1, ticks=1)


def test_borrar_uses_borrar_endpoint_and_shares_classifier() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/athlete/handlers/Calendario_Borrar.ashx"
        assert request.url.params["id"] == "999"
        assert request.url.params["ticks"] == "42"
        return _booking_ok_response("Ok")

    result = _client(httpx.MockTransport(handler)).borrar(
        cookie_value="c", class_id="999", ticks=42
    )
    assert result.outcome == "granted"
    assert result.raw_res == "Ok"


def test_borrar_accepts_int_class_id_and_stringifies() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["id"] = request.url.params["id"]
        return _booking_ok_response("Ok")

    _client(httpx.MockTransport(handler)).borrar(cookie_value="c", class_id=555, ticks=1)
    # httpx serialises ints to str; the client must not stringify twice
    # or lose the value in a numeric edge (leading zeros etc).
    assert captured["id"] == "555"


# ---------------------------------------------------------------------------
# discover_idu — self-idu discovery for the add-gym flow (FR-011)
# ---------------------------------------------------------------------------

_MENU_AVATAR_HTML = (
    '<html><body><nav><img class="avatar inmenu" '
    'src="https://cdn.wodbuster.com/static/atletas/a/a/e/'
    'aae990e4-fa58-4cfc-894d-e204f0e37605.jpg?t=6388420596108"></nav>'
    "<div>reservas</div></body></html>"
)


def _discovery_client(handler: httpx.MockTransport) -> WodBusterClient:
    """Build a discovery-only client (gym known, idu not yet)."""
    http_client = httpx.Client(transport=handler, follow_redirects=False)
    return WodBusterClient(gym="newgym", http_client=http_client)


def test_discover_idu_reads_self_idu_from_menu_avatar() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.host == "newgym.wodbuster.com"
        assert request.url.path == "/athlete/reservas.aspx"
        assert request.headers["cookie"] == ".WBAuth=fresh-cookie"
        return httpx.Response(
            200,
            headers={"content-type": "text/html; charset=utf-8"},
            content=_MENU_AVATAR_HTML.encode(),
        )

    idu = _discovery_client(httpx.MockTransport(handler)).discover_idu("fresh-cookie")
    assert idu == "aae990e4fa584cfc894de204f0e37605"


def test_discover_idu_rejected_cookie_raises_auth_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={"location": "/account/login.aspx?ReturnUrl=%2f"})

    with pytest.raises(WodBusterAuthError):
        _discovery_client(httpx.MockTransport(handler)).discover_idu("stale")


def test_discover_idu_no_avatar_raises_protocol_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/html"},
            content=b"<html><body>no menu here</body></html>",
        )

    with pytest.raises(WodBusterProtocolError, match="idu"):
        _discovery_client(httpx.MockTransport(handler)).discover_idu("cookie")


def test_idu_scoped_call_on_discovery_client_fails_loudly() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"{}")

    client = _discovery_client(httpx.MockTransport(handler))
    with pytest.raises(RuntimeError, match="discovery-only"):
        client.load_class(cookie_value="c", ticks=1)
