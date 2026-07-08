"""Projected cookie TTL estimator (US3.4).

Given a heartbeat verdict and the previous projection, returns the new
projected expiry timestamp for the operator's cookie. Pure function; the
persistence-side write is the caller's job.

Design rules (from tasks.md US3.4 and its US4.T5 counterpart):

- **Ceiling.** A ``Valid`` probe assumes the cookie could last at most
  ``ceiling`` (default 30 days per plan). The candidate expiry is
  ``now + ceiling``.
- **Monotonic non-increasing between paste events.** Consecutive
  ``Valid`` probes never push the projection further into the future
  than the previous one. This models cookie lifetime pessimistically:
  once we suspect the cookie will die at ``T``, a later probe cannot
  reset optimism to ``T + delta``.
- **Reset happens at paste, not at probe.** :class:`CookieStore.save`
  already clears ``cookie_credential.projected_ttl_at`` on every
  successful paste. The next :class:`HeartbeatProbe` cycle then sees
  ``previous=None`` and starts a fresh ceiling.
- **Rejected → immediate expiry.** A server rejection means the cookie
  is gone right now; projection is ``now``. Downstream alerters
  (US4.3) turn this into a ``cookie_expiring`` (or ``cookie_invalid``)
  alert on the same cycle.
- **Unknown → no change.** A transient transport / protocol failure is
  not evidence one way or the other. Keep the previous projection.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from ..security.cookie import Rejected, Unknown, Valid, ValidationResult


def project_ttl(
    *,
    verdict: ValidationResult,
    now: datetime,
    ceiling: timedelta,
    previous: datetime | None,
) -> datetime | None:
    """Return the new projected expiry for the operator's cookie.

    Callers pass ``now`` explicitly so unit tests can pin the clock
    without monkeypatching ``datetime``. The persistence layer picks
    the value up and writes it to ``cookie_credential.projected_ttl_at``.
    """
    if isinstance(verdict, Rejected):
        return now
    if isinstance(verdict, Unknown):
        return previous
    if isinstance(verdict, Valid):
        candidate = now + ceiling
        if previous is None:
            return candidate
        # ``min`` on aware datetimes compares by absolute instant which
        # is what we want (never let the projection grow).
        return min(candidate, previous)
    raise TypeError(f"unsupported verdict type: {type(verdict).__name__}")


__all__ = ["project_ttl"]
