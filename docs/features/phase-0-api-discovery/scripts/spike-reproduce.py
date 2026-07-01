"""
Phase 0 API discovery spike: reproduction script.

This script tests whether a session cookie obtained from a real browser
login can be replayed by a non-browser HTTP client to drive the
WodBuster booking endpoints. It is the operational artifact of Step 5
in the plan, and the source of the latency data for Step 6.

DESIGN: cookie handoff. The owner exports the `Cookie` header from a
logged-in browser session and saves it to a local file. This script
loads that cookie header, attaches it to a `requests.Session()`, and
calls the WodBuster handlers discovered in the Step 2 HAR.

Two phases:

1. READ-ONLY sanity check (always runs). Issues a GET to
   `LoadClass.ashx` for today's date. A `200` JSON response confirms
   the cookie replay works (closes assumption A6 in the feasibility
   report).

2. BOOKING call (gated). Issues a GET to `Calendario_Inscribir.ashx`
   for an operator-specified class id. Disabled by default; enable by
   setting `WB_BOOK=1` and supplying `WB_CLASS_ID` and `WB_CLASS_TICKS`.
   Counts against the FR-009 quota of 2 real bookings for the entire
   spike.

Safety rules:

- The cookie header is read from a file outside the repo. It is never
  echoed in full. Only cookie names and value lengths are printed.
- The booking call is gated behind an explicit env var and prints a
  dry-run summary first.
- The script honors the FR-009 polite client quotas at the operator
  level (the operator counts invocations).

Usage (PowerShell on Windows):

    $env:WB_COOKIES_FILE = "C:\\spike-captures\\cookie-header.txt"
    $env:WB_IDU          = "aae990e4fa584cfc894de204f0e37605"  # from Step 2 HAR
    $env:WB_GYM          = "antworktrainingcenter"
    python spike-reproduce.py

    # When ready to attempt a real booking (FR-009 quota applies):
    $env:WB_BOOK         = "1"
    $env:WB_CLASS_ID     = "45654"             # from a prior LoadClass response
    $env:WB_CLASS_TICKS  = "1782864000"        # 00:00 UTC of the class date
    python spike-reproduce.py

    # If phase 2a (no connectionId) fails, escalate by setting:
    $env:WB_USE_SIGNALR  = "1"                 # negotiate a SignalR token first
    python spike-reproduce.py
"""

from __future__ import annotations

import os
import sys
import time
from datetime import datetime, timezone
from typing import Any

import requests  # pip install requests

CONNECT_TIMEOUT_S = 5
READ_TIMEOUT_S = 10


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        print(f"error: environment variable {name} is not set", file=sys.stderr)
        sys.exit(2)
    return value


def _optional_env(name: str) -> str | None:
    value = os.environ.get(name)
    return value if value else None


def _summarize_cookie(name: str, value: str) -> str:
    return f"  - {name}  [str(len={len(value)})]"


def _load_cookie_header(path: str) -> dict[str, str]:
    """
    Read a Cookie header line and parse it into name -> value pairs.

    Accepts either:
      - the raw value of a Cookie request header
        (for example `ASP.NET_SessionId=abc; .ASPXAUTH=def; cf_clearance=...`)
      - the same value with a leading `Cookie:` label, which is stripped
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        print(f"error: cookie file not found: {path}", file=sys.stderr)
        sys.exit(2)
    if not raw:
        print(f"error: cookie file is empty: {path}", file=sys.stderr)
        sys.exit(2)
    if raw.lower().startswith("cookie:"):
        raw = raw.split(":", 1)[1].strip()
    cookies: dict[str, str] = {}
    for part in raw.split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        cookies[name.strip()] = value.strip()
    if not cookies:
        print(
            "error: cookie file parsed to zero cookies. "
            "Expected `name=value; name=value; ...` on one line.",
            file=sys.stderr,
        )
        sys.exit(2)
    return cookies


def _build_session(cookies: dict[str, str]) -> requests.Session:
    session = requests.Session()
    # The auth cookie is set on `.wodbuster.com` so it scopes to the gym
    # subdomain too. We replay it on the apex domain; requests will send
    # it on subdomain requests automatically because the cookie store
    # treats the parent domain as a match.
    for name, value in cookies.items():
        session.cookies.set(name, value, domain=".wodbuster.com", path="/")
    # Polite identification. The HAR was captured with a normal browser
    # user-agent; using a clearly non-default value here makes the
    # spike's footprint distinguishable in any access log inspection
    # the operator may need to do.
    session.headers.update(
        {
            "User-Agent": "wodbuster-booking-scheduler/0.1 (phase-0 spike; contact: operator)",
            "Accept": "application/json, text/json, */*",
            "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
        }
    )
    return session


def _today_ticks_utc() -> int:
    """Return the UNIX timestamp of today at 00:00 UTC."""
    now = datetime.now(timezone.utc)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


def _cachebuster() -> int:
    """A millisecond timestamp, matching the `_=` query parameter the WB UI uses."""
    return int(time.time() * 1000)


SIGNALR_NEGOTIATE_URL = (
    "https://sr-3-4.wodbuster.com/bookinghub/negotiate?negotiateVersion=1"
)


def negotiate_signalr(session: requests.Session, gym: str) -> str | None:
    """
    Phase 2b helper: open a one-shot SignalR negotiate exchange and return
    the resulting connection id, which can then be passed to the booking
    call as the `connectionId` query parameter.

    This is needed only if `Calendario_Inscribir.ashx` rejects calls
    without `connectionId` (assumption A4). For phase 2a we deliberately
    skip this helper and book without `connectionId`. If that fails the
    script will call this function on the retry.

    Does not open a WebSocket. SignalR's negotiate is a plain POST; the
    upgrade to WebSocket is a separate handshake that the production
    worker may or may not need. We only need the id, not the live channel.
    """
    # The negotiate endpoint lives on `sr-3-4.wodbuster.com`, a different
    # subdomain. The `.WBAuth` cookie set on `.wodbuster.com` is sent
    # automatically because requests propagates parent-domain cookies.
    # We add browser-realistic CORS hints because the WB UI calls
    # negotiate from the gym subdomain.
    origin = f"https://{gym}.wodbuster.com"
    headers = {
        "Origin": origin,
        "Referer": f"{origin}/athlete/reservas.aspx",
        "X-Requested-With": "XMLHttpRequest",
        "Content-Length": "0",
    }
    print(f"\n[signalr] negotiate")
    print(f"  url:         {SIGNALR_NEGOTIATE_URL}")
    start = time.perf_counter()
    response = session.post(
        SIGNALR_NEGOTIATE_URL,
        headers=headers,
        timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S),
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"  status:      {response.status_code}")
    print(f"  latency_ms:  {elapsed_ms:.0f}")
    print(f"  resp_mime:   {response.headers.get('Content-Type', '')}")
    print(f"  resp_size:   {len(response.content)} bytes")
    if response.status_code != 200:
        print("  -> negotiate failed. Cannot obtain a connectionId.")
        return None
    try:
        body = response.json()
    except Exception:
        print("  -> negotiate response is not JSON.")
        return None
    if not isinstance(body, dict):
        print(f"  -> unexpected negotiate body type: {type(body).__name__}")
        return None
    # ASP.NET Core SignalR negotiate returns either `connectionId` (older
    # protocol) or `connectionToken` (newer). The booking handler's
    # `connectionId` query param matches whichever string identifies the
    # connection on the server. We log both and prefer `connectionToken`
    # if present (the newer protocol is what the WB UI uses based on the
    # `negotiateVersion=1` query param).
    print(f"  -> negotiate keys: {sorted(body.keys())}")
    connection_token = body.get("connectionToken")
    connection_id = body.get("connectionId")
    chosen = connection_token or connection_id
    if chosen:
        print(f"  -> connection token obtained (len={len(chosen)}).")
    else:
        print("  -> no connection token in negotiate response.")
    return chosen


def load_class(
    session: requests.Session, gym: str, idu: str, ticks: int
) -> dict[str, Any] | None:
    """
    Phase 1: read-only sanity check.

    Calls `GET /athlete/handlers/LoadClass.ashx` to confirm the cookie
    handoff produces an authenticated, parseable response. Does not
    mutate any state on WodBuster.
    """
    url = f"https://{gym}.wodbuster.com/athlete/handlers/LoadClass.ashx"
    params = {
        "ticks": ticks,
        "idu": idu,
        "_": _cachebuster(),
    }
    start = time.perf_counter()
    response = session.get(
        url, params=params, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S)
    )
    elapsed_ms = (time.perf_counter() - start) * 1000

    print(f"\n[phase 1] LoadClass.ashx")
    print(f"  url:         {url}")
    print(
        f"  ticks:       {ticks}  ({datetime.fromtimestamp(ticks, tz=timezone.utc).isoformat()})"
    )
    print(f"  status:      {response.status_code}")
    print(f"  latency_ms:  {elapsed_ms:.0f}")
    print(f"  resp_mime:   {response.headers.get('Content-Type', '')}")
    print(f"  resp_size:   {len(response.content)} bytes")

    if response.status_code != 200:
        print(f"  -> non-200 response. Aborting phase 1.")
        return None

    # Try to parse as JSON without leaking content. We print only the
    # top-level shape (keys for an object, length for a list).
    try:
        data = response.json()
    except Exception as exc:
        print(
            f"  -> response is not JSON ({exc.__class__.__name__}). "
            f"This is unexpected and indicates the auth replay may have failed. "
            f"If the response is HTML, the server probably served a login page."
        )
        return None

    if isinstance(data, list):
        print(f"  -> JSON list, length={len(data)}")
    elif isinstance(data, dict):
        print(f"  -> JSON object, top-level keys: {sorted(data.keys())}")
    else:
        print(f"  -> JSON scalar of type {type(data).__name__}")
    print(f"  -> cookie handoff confirmed working.")
    return data


def book(
    session: requests.Session,
    gym: str,
    idu: str,
    class_id: str,
    ticks: int,
    connection_id: str | None,
) -> None:
    """
    Phase 2: REAL booking call. Gated by WB_BOOK=1.

    Counts against the FR-009 quota of 2 real bookings for the entire
    spike. The operator must independently track invocations.
    """
    url = f"https://{gym}.wodbuster.com/athlete/handlers/Calendario_Inscribir.ashx"
    params: dict[str, Any] = {
        "id": class_id,
        "ticks": ticks,
        "idu": idu,
        "_": _cachebuster(),
    }
    if connection_id:
        params["connectionId"] = connection_id

    print(f"\n[phase 2] Calendario_Inscribir.ashx")
    print(f"  url:           {url}")
    print(f"  class_id:      {class_id}")
    print(
        f"  ticks:         {ticks}  ({datetime.fromtimestamp(int(ticks), tz=timezone.utc).isoformat()})"
    )
    print(f"  connectionId:  {'<set>' if connection_id else '<omitted>'}")

    if os.environ.get("WB_BOOK") != "1":
        print(f"  -> DRY RUN (WB_BOOK is not set to '1'). No request was issued.")
        print(f"  -> To execute: set WB_BOOK=1 and re-run. Remember the FR-009 quota.")
        return

    start = time.perf_counter()
    response = session.get(
        url, params=params, timeout=(CONNECT_TIMEOUT_S, READ_TIMEOUT_S)
    )
    elapsed_ms = (time.perf_counter() - start) * 1000
    print(f"  status:        {response.status_code}")
    print(f"  latency_ms:    {elapsed_ms:.0f}")
    print(f"  resp_mime:     {response.headers.get('Content-Type', '')}")
    print(f"  resp_size:     {len(response.content)} bytes")
    if response.status_code != 200:
        print(
            f"  -> non-200 response. Inspect the WodBuster UI to confirm no booking was made."
        )
        return
    try:
        data = response.json()
        if isinstance(data, dict):
            print(f"  -> JSON object, top-level keys: {sorted(data.keys())}")
        elif isinstance(data, list):
            print(f"  -> JSON list, length={len(data)}")
    except Exception:
        print(f"  -> response is not JSON.")
    print(
        f"  -> booking call returned. Verify in the WodBuster UI that the booking is registered."
    )


def main() -> int:
    cookies_file = _require_env("WB_COOKIES_FILE")
    idu = _require_env("WB_IDU")
    gym = _require_env("WB_GYM")

    print(f"cookies_file:  {cookies_file}")
    print(f"gym:           {gym}")
    print(f"idu (len):     {len(idu)}")

    cookies = _load_cookie_header(cookies_file)
    print(f"\nparsed {len(cookies)} cookie(s) from header (names only):")
    for name, value in cookies.items():
        print(_summarize_cookie(name, value))

    session = _build_session(cookies)
    ticks = _today_ticks_utc()
    result = load_class(session, gym, idu, ticks)
    if result is None:
        print("\nphase 1 failed. Not proceeding to phase 2.")
        return 1

    class_id = _optional_env("WB_CLASS_ID")
    class_ticks = _optional_env("WB_CLASS_TICKS")
    if class_id and class_ticks:
        connection_id = _optional_env("WB_CONNECTION_ID")
        # Phase 2 escalation path: if the operator has set WB_USE_SIGNALR=1
        # and has not supplied an explicit connection id, run the negotiate
        # exchange now and use the resulting token.
        if not connection_id and os.environ.get("WB_USE_SIGNALR") == "1":
            connection_id = negotiate_signalr(session, gym)
            if not connection_id:
                print(
                    "\nSignalR negotiate did not return a connection id. "
                    "Proceeding to phase 2 without one."
                )
        book(session, gym, idu, class_id, int(class_ticks), connection_id)
    else:
        print(
            "\nphase 2 not requested (WB_CLASS_ID and WB_CLASS_TICKS were not both set). "
            "Phase 1 succeeded, which already confirms the cookie handoff design is viable."
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
