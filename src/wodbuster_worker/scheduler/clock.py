"""Operator-clock primitives shared by the scheduler, the booking path
and the class probe.

Dependency-free on purpose (standard library only). These three names
used to live in :mod:`scheduler.rule_jobs`, which pulls in APScheduler
and the booking executor, so every other module reached them through a
function-level import to dodge a circular import. Hosting them in a leaf
module lets callers import at module scope instead, which is what the
single-day override path needs: ``booking.overrides`` is imported by both
``rule_jobs`` and ``booking.executor``.

``rule_jobs`` re-exports ``operator_timezone`` and
``DEFAULT_PREWARM_LEAD_S`` so existing import sites keep working.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from zoneinfo import ZoneInfo

# US1.4 pre-warm lead. Schedule each booking job this many seconds
# before the operator-facing ``booking_opens_at`` moment. The executor
# uses the head start to poll ``SegundosHastaPublicacion`` (US1.5) and
# warm the httpx keep-alive pool so the ``inscribir`` call rides on a
# live TCP + TLS session.
DEFAULT_PREWARM_LEAD_S = 30.0


def operator_timezone() -> ZoneInfo:
    """Return the timezone in which every rule's ``HH:MM`` is interpreted.

    Reads ``WORKER_TIMEZONE`` from the environment (default
    ``Europe/Madrid``). Kept as a lazy lookup so tests can override via
    ``monkeypatch.setenv``. The gym runs on the operator's local clock;
    treating ``HH:MM`` as UTC (as an earlier draft did) fires the
    scheduler at the wrong instant.
    """
    return ZoneInfo(os.environ.get("WORKER_TIMEZONE", "Europe/Madrid"))


def midnight_utc_ticks(target_slot: datetime) -> int:
    """Return the UTC-midnight epoch seconds for ``target_slot``'s day.

    Phase 0 established that LoadClass and the booking handlers accept a
    ``ticks`` parameter equal to the UTC-midnight seconds-since-epoch of
    the target date. This is the executor's convention, and the
    date-scoped class probe deliberately adopts it (plan AMB-005) so a
    validated combination is checked against the same calendar day the
    booking attempt will read.
    """
    aware = target_slot.astimezone(UTC)
    midnight = aware.replace(hour=0, minute=0, second=0, microsecond=0)
    return int(midnight.timestamp())


__all__ = [
    "DEFAULT_PREWARM_LEAD_S",
    "midnight_utc_ticks",
    "operator_timezone",
]
