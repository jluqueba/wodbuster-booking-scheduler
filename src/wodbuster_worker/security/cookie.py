"""Cookie validator for US-003 paste-and-validate flow.

Given a candidate ``.WBAuth`` value, calls WodBuster once and classifies
the result into one of three verdicts that the paste-form view uses to
decide whether to persist, reject with detail, or show a retry prompt:

- :class:`Valid` — cookie accepted, safe to persist.
- :class:`Rejected` — cookie rejected by the server. The banner tells
  the operator to re-copy from the browser.
- :class:`Unknown` — the probe itself failed (transport, protocol). The
  banner tells the operator to try again in a minute. **No state
  mutation must happen on this verdict** (FR-020): a transient network
  glitch is not evidence that the pasted value is wrong.

The classification lives here (not in the HTTP client) because the
mapping from exception type to operator-facing verdict is a UX policy,
not a protocol detail. Tests can drive the validator through a fake
client that raises any of the three exception classes.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from ..wodbuster_client.client import (
    WodBusterAuthError,
    WodBusterClientProtocol,
    WodBusterProtocolError,
    WodBusterTransportError,
)


@dataclass(frozen=True)
class Valid:
    """The cookie authenticated successfully against WodBuster.

    ``probed_at`` is the wall clock at the moment the probe returned;
    :class:`CookieStore.save` copies it to ``cookie_credential.last_validated_at``.
    """

    probed_at: datetime


@dataclass(frozen=True)
class Rejected:
    """The server refused the cookie.

    ``reason`` is a short operator-facing string suitable for a banner
    ("cookie expired or was revoked"). It does not include diagnostic
    URLs, trace IDs, or upstream error messages.
    """

    reason: str


@dataclass(frozen=True)
class Unknown:
    """The probe itself failed and the cookie's validity is undetermined.

    ``reason`` describes the probe failure so the operator knows
    whether to retry immediately (transient network) or wait
    (protocol error suggests server-side degradation).
    """

    reason: str


ValidationResult = Valid | Rejected | Unknown


class CookieValidator:
    """Classify a pasted ``.WBAuth`` cookie via a single WodBuster probe.

    Construct once with a shared :class:`WodBusterClient`. The
    validator itself is stateless.
    """

    __slots__ = ("_client",)

    def __init__(self, client: WodBusterClientProtocol) -> None:
        self._client = client

    def validate(self, cookie_value: str) -> ValidationResult:
        """Probe the cookie and return the classified verdict.

        A blank value short-circuits to :class:`Rejected` without
        contacting the server — the paste form should never submit an
        empty string, and if it does, the answer is deterministic.
        """
        if not cookie_value or not cookie_value.strip():
            return Rejected(reason="cookie value is empty")

        # Use "today at 00:00 UTC" as the probe timestamp. Phase 0's
        # LoadClass returns a filtered calendar for the requested week;
        # today's midnight is always a legal argument.
        ticks = _today_ticks_utc()

        try:
            self._client.load_class(cookie_value, ticks)
        except WodBusterAuthError as exc:
            return Rejected(reason=f"server rejected cookie ({exc})")
        except WodBusterTransportError as exc:
            return Unknown(reason=f"could not reach WodBuster ({exc})")
        except WodBusterProtocolError as exc:
            return Unknown(reason=f"unexpected response from WodBuster ({exc})")

        return Valid(probed_at=datetime.now(tz=UTC))


def _today_ticks_utc() -> int:
    """Unix timestamp of today's 00:00 UTC. Matches Phase 0's ticks convention."""
    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


__all__ = [
    "CookieValidator",
    "Rejected",
    "Unknown",
    "Valid",
    "ValidationResult",
]
