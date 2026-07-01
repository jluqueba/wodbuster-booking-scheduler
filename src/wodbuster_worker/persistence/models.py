"""SQLAlchemy models for the WodBuster worker.

One module per plan; each table becomes one declaratively mapped class.
Column choices follow ``docs/features/wodbuster-booking-worker/plan.md``
Data Model section and ADR-0002 (SQLite on Azure Files with
application-layer AES-256-GCM encryption for cookie material).

Design notes:

- All timestamps are ``DateTime(timezone=True)``. SQLite stores them
  as ISO strings; SQLAlchemy handles conversion.
- Enum-like status columns use ``sa.Enum(..., native_enum=False)`` so
  they render as ``VARCHAR + CHECK`` on SQLite (native enums are not
  portable there).
- Ciphertext and nonce columns are ``LargeBinary``. Plaintext columns
  for any secret material are forbidden by ADR-0002.
- The ``alert`` table enforces at most one open row per
  ``(operator_id, kind)`` via a partial unique index. SQLite supports
  this via ``sqlite_where``.
- Foreign keys are declared but SQLite only enforces them when the
  ``PRAGMA foreign_keys=ON`` is set on the connection; that pragma is
  applied in ``engine.py`` via an event listener.
"""

from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base

# --- Enum vocabularies ------------------------------------------------------
#
# Kept as tuples of strings rather than Python ``enum.Enum`` classes so
# that database rows read as plain strings without an application-layer
# coercion step. Application code that wants type-safe access can wrap
# these values in Literal types at the call site.

_PROVIDERS = ("microsoft", "github", "google")
_COOKIE_PROBE_STATUSES = ("valid", "rejected", "unknown")
_BOOKING_TERMINAL_STATUSES = (
    "granted",
    "full",
    "cookie_invalid",
    "class_not_visible",
    "upstream_unavailable",
    "cancelled",
)
_HEARTBEAT_RESULTS = ("valid", "rejected", "unknown")
# The alert kinds intentionally cover only the vocabularies referenced in
# the plan and spec (cookie expiring, cookie invalid, silent-run
# heartbeat anomaly). New kinds land with the story that emits them.
_ALERT_KINDS = (
    "cookie_expiring",
    "cookie_invalid",
    "heartbeat_anomaly",
)
_NOTIFICATION_KINDS = ("telegram", "banner")


class OperatorProfile(Base):
    """The single human user (spec Key Entities → Operator).

    Multi-tenant scale is out of scope. The table exists so that every
    downstream row can carry an ``operator_id`` foreign key, which
    keeps invariants (one open alert per kind, one cookie per
    operator) expressible.
    """

    __tablename__ = "operator_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Optional until the operator binds Telegram via /start (US-007).
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class FederatedIdentity(Base):
    """OAuth identities allow-listed for a single operator (ADR-0005).

    A row is created by the bootstrap command described in the plan
    (``python -m wodbuster_worker.bootstrap``). The unique key on
    ``(provider, subject_id)`` prevents the same external identity
    from binding to two operators.
    """

    __tablename__ = "federated_identity"
    __table_args__ = (
        UniqueConstraint("provider", "subject_id", name="uq_federated_identity_provider_subject"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(
        Enum(*_PROVIDERS, name="provider_enum", native_enum=False), nullable=False
    )
    subject_id: Mapped[str] = mapped_column(String(256), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    # AES-256-GCM ciphertext of the OAuth refresh token, when a provider
    # issues one. Null until a token is captured. ADR-0005 forbids any
    # plaintext refresh-token column.
    refresh_token_ciphertext: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    refresh_token_nonce: Mapped[bytes | None] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SchedulerRule(Base):
    """Recurring weekly booking intent (FR-002)."""

    __tablename__ = "scheduler_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # 0 = Monday .. 6 = Sunday (Python ``datetime.weekday()`` convention).
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)
    # How many hours before the class the booking window opens. Real-world
    # values are small integers (24, 48, 72). Modelled as Integer.
    # TODO: plan is silent on whether this can be fractional. Kept
    # integer for now; widen to Numeric if a case emerges.
    window_offset_hours: Mapped[int] = mapped_column(Integer, nullable=False)
    active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.true()
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    preferences: Mapped[list[ClassPreference]] = relationship(
        "ClassPreference",
        back_populates="rule",
        cascade="all, delete-orphan",
        order_by="ClassPreference.order_index",
    )


class ClassPreference(Base):
    """Ordered fallbacks inside a scheduler rule (FR-009)."""

    __tablename__ = "class_preference"
    __table_args__ = (
        UniqueConstraint("rule_id", "order_index", name="uq_class_preference_rule_order"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("scheduler_rule.id", ondelete="CASCADE"), nullable=False, index=True
    )
    order_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # Free-form label taken from WodBuster (e.g. "WOD", "Halterofilia").
    # TODO: plan is silent on canonical class-type vocabulary. Modelled
    # as free String; tighten to Enum if a fixed vocabulary appears.
    class_type: Mapped[str] = mapped_column(String(100), nullable=False)
    # ISO ``HH:MM`` (24h) representation of the target slot start time.
    target_time_slot: Mapped[str] = mapped_column(String(5), nullable=False)

    rule: Mapped[SchedulerRule] = relationship("SchedulerRule", back_populates="preferences")


class CookieCredential(Base):
    """Encrypted ``.WBAuth`` blob (ADR-0002, ADR-0005, FR-020).

    One row per operator represents the active cookie. The paste-and-
    validate flow (US-003) upserts on ``operator_id``; historic values
    are not retained because the plaintext must never survive rotation.
    """

    __tablename__ = "cookie_credential"
    __table_args__ = (
        UniqueConstraint("operator_id", name="uq_cookie_credential_operator"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"), nullable=False
    )
    cookie_ciphertext: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    cookie_nonce: Mapped[bytes] = mapped_column(LargeBinary, nullable=False)
    pasted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_validated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    projected_ttl_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_probe_status: Mapped[str | None] = mapped_column(
        Enum(*_COOKIE_PROBE_STATUSES, name="cookie_probe_status_enum", native_enum=False),
        nullable=True,
    )


class BookingOutcome(Base):
    """One row per booking attempt (FR-012, plan sequence diagram)."""

    __tablename__ = "booking_outcome"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Manual ad-hoc bookings (FR-018) have no rule; keep nullable.
    rule_id: Mapped[int | None] = mapped_column(
        ForeignKey("scheduler_rule.id", ondelete="SET NULL"), nullable=True
    )
    target_class: Mapped[str] = mapped_column(String(100), nullable=False)
    target_slot: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    attempted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    terminal_status: Mapped[str] = mapped_column(
        Enum(
            *_BOOKING_TERMINAL_STATUSES,
            name="booking_terminal_status_enum",
            native_enum=False,
        ),
        nullable=False,
    )
    # 0-based index into the ``class_preference`` walk that produced the
    # granted outcome. Null when the terminal status is not ``granted``.
    granted_fallback_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Full response payload retained for post-mortem (FR-012). Stored as
    # text; the WodBuster response is small (< 4 KB per Phase 0).
    response_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VacationWindow(Base):
    """Date range with skip-and-cancel semantics (FR-015)."""

    __tablename__ = "vacation_window"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Date-only fields modelled as DateTime to avoid the SQLite Date
    # affinity edge cases; the time component is always midnight UTC.
    # TODO: plan says "date range". Kept as DateTime here; if a UI
    # exposes date-only pickers, add a Date column and let SQLAlchemy
    # coerce.
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class HeartbeatReading(Base):
    """One row per cookie probe (FR-022, ADR-0006)."""

    __tablename__ = "heartbeat_reading"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    probed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    result: Mapped[str] = mapped_column(
        Enum(*_HEARTBEAT_RESULTS, name="heartbeat_result_enum", native_enum=False),
        nullable=False,
    )
    projected_ttl_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set to the alert row that this heartbeat produced, if any (FR-023
    # 24h-lead alert or FR-011 cookie-invalid). Null when the reading
    # produced no alert.
    alert_id: Mapped[int | None] = mapped_column(
        ForeignKey("alert.id", ondelete="SET NULL"), nullable=True
    )


class Alert(Base):
    """Operator-facing condition (spec Key Entities → Alert).

    Invariant: at most one *open* (``closed_at IS NULL``) row per
    ``(operator_id, kind)``. Enforced with a partial unique index; the
    ``sqlite_where`` argument produces valid SQLite DDL.
    """

    __tablename__ = "alert"
    __table_args__ = (
        Index(
            "uq_alert_open_operator_kind",
            "operator_id",
            "kind",
            unique=True,
            sqlite_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(
        Enum(*_ALERT_KINDS, name="alert_kind_enum", native_enum=False), nullable=False
    )
    # Free-form JSON blob describing the alert. Kept as text so
    # applications can serialize with their own schema.
    payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationOutbox(Base):
    """Pending delivery to Telegram or the web banner pool.

    Every state-mutating write that produces an operator-visible signal
    writes an outbox row in the same transaction (plan cross-cutting
    rule). A dispatcher polls this table.
    """

    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    operator_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(
        Enum(*_NOTIFICATION_KINDS, name="notification_kind_enum", native_enum=False),
        nullable=False,
    )
    # Provider-scoped target: Telegram chat id, or a UI channel label
    # for the banner pool.
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    payload: Mapped[str] = mapped_column(Text, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )


__all__ = [
    "Alert",
    "Base",
    "BookingOutcome",
    "ClassPreference",
    "CookieCredential",
    "FederatedIdentity",
    "HeartbeatReading",
    "NotificationOutbox",
    "OperatorProfile",
    "SchedulerRule",
    "VacationWindow",
]
