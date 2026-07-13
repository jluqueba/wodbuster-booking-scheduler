"""Booking core (US-001).

Two collaborating pieces:

- :class:`BookingExecutor` — orchestrates one booking attempt for a
  scheduler rule. Fetches the operator's LoadClass view, matches the
  primary class (retrying if not yet visible), fires
  ``inscribir``, walks to the second shot when the primary is full,
  and hands the terminal outcome to the writer.
- :func:`persist_outcome` — writes one ``booking_outcome`` row plus a
  paired ``notification_outbox`` row in a single transaction (spec
  cross-cutting rule "no operator-visible signal without a durable
  row").

Kept in one package so the executor can rely on the writer's
transactional contract without leaking session-management concerns
into the surrounding modules.
"""

from __future__ import annotations

from .executor import BookingExecutor, BookingResult
from .outcomes import persist_outcome

__all__ = [
    "BookingExecutor",
    "BookingResult",
    "persist_outcome",
]
