"""Persistence layer public surface.

Re-exports the declarative ``Base`` and the model classes so callers can
``from wodbuster_worker.persistence import Base, OperatorProfile`` without
reaching into private modules. Also re-exports the engine / session
factory once wired.
"""

from __future__ import annotations

from .base import Base
from .engine import build_engine, get_engine, get_session, reset_engine
from .models import (
    Alert,
    BookingOutcome,
    CookieCredential,
    FederatedIdentity,
    HeartbeatReading,
    NotificationOutbox,
    OperatorProfile,
    SchedulerRule,
    VacationWindow,
)

__all__ = [
    "Alert",
    "Base",
    "BookingOutcome",
    "CookieCredential",
    "FederatedIdentity",
    "HeartbeatReading",
    "NotificationOutbox",
    "OperatorProfile",
    "SchedulerRule",
    "VacationWindow",
    "build_engine",
    "get_engine",
    "get_session",
    "reset_engine",
]
