"""Healthchecks.io ping wrapper (US2.5, FR-025, ADR-0006).

External dead-man monitor. Every 10 minutes the scheduler calls
:func:`ping` against the URL stored in the ``healthchecks-ping-url``
Key Vault secret. Healthchecks.io accepts POST or GET; a successful
2xx response resets the check's timer. If the worker crashes,
loses network, or the container is stuck restarting for more than
the check's grace period, Healthchecks.io fires an alert on its
own channels (Telegram) — an alarm that cannot share fate with the
Azure region hosting the worker.

Failures are logged and swallowed. A single missed ping is
tolerated by design; the check's grace period (20 minutes for a
10-minute cadence) absorbs one blip. Repeated failures either
recover (subsequent pings land) or trip the dead-man alert —
either way the pinger stays out of the way and never raises into
the scheduler.
"""

from __future__ import annotations

import httpx
import structlog

_log = structlog.get_logger(__name__)

_DEFAULT_TIMEOUT_S: float = 5.0


def ping(
    url: str,
    *,
    client: httpx.Client | None = None,
    timeout_s: float = _DEFAULT_TIMEOUT_S,
) -> bool:
    """POST to ``url``; return True on 2xx, False on any failure.

    ``client`` is injectable so tests can pass an
    ``httpx.MockTransport``-backed client. Production callers pass
    ``None`` and the function opens (and closes) its own client.

    Never raises. All error paths log at ``warning`` and return
    ``False`` — the scheduler wrapper treats a False return as
    "will try again on the next tick", which is the same behaviour
    as a genuine transport failure.
    """
    owned_client = client is None
    http = client or httpx.Client(timeout=timeout_s)
    try:
        try:
            response = http.post(url)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            _log.warning("healthchecks.ping.transport_error", error=str(exc))
            return False
        if 200 <= response.status_code < 300:
            _log.debug("healthchecks.ping.ok", status=response.status_code)
            return True
        _log.warning(
            "healthchecks.ping.unexpected_status",
            status=response.status_code,
        )
        return False
    finally:
        if owned_client:
            http.close()


__all__ = ["ping"]
