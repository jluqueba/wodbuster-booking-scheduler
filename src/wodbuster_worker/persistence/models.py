"""SQLAlchemy models for the WodBuster worker.

One module per plan; each table becomes one declaratively mapped class.
Column choices follow ``docs/features/wodbuster-booking-worker/plan.md``
Data Model section and ADR-0002 (Postgres 16 on Azure Database for
PostgreSQL Flexible Server with application-layer AES-256-GCM
encryption for cookie material).

Design notes:

- All timestamps are ``DateTime(timezone=True)``, which renders as
  ``TIMESTAMPTZ`` on Postgres.
- Enum-like columns use ``sa.Enum(..., native_enum=True)``, which
  translates to a real Postgres ``CREATE TYPE`` under the hood. Values
  are the same string vocabularies the application sees.
- Ciphertext and nonce columns are ``LargeBinary`` (``BYTEA`` on
  Postgres). Plaintext columns for any secret material are forbidden
  by ADR-0002.
- The ``alert`` table enforces at most one open row per
  ``(gym_account_id, kind)`` via a partial unique index rendered through
  ``postgresql_where``.
- Foreign keys are enforced natively; no per-connection pragmas are
  needed (contrast the historical SQLite implementation).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

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
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base

# --- Enum vocabularies ------------------------------------------------------
#
# Kept as tuples of strings rather than Python ``enum.Enum`` classes so
# that database rows read as plain strings without an application-layer
# coercion step. Application code that wants type-safe access can wrap
# these values in Literal types at the call site.
#
# The tuples are passed positionally to ``sa.Enum``; ``native_enum=True``
# tells SQLAlchemy to emit a Postgres ``CREATE TYPE ... AS ENUM (...)``
# for each named enum below.

_PROVIDERS = ("microsoft", "github", "google")
_COOKIE_PROBE_STATUSES = ("valid", "rejected", "unknown")
_BOOKING_TERMINAL_STATUSES = (
    "granted",
    "full",
    "cookie_invalid",
    "class_not_visible",
    "upstream_unavailable",
    "cancelled",
    "skipped",
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
# Communication language for the operator (User Profile, ADR-0008). Governs
# Telegram message rendering and the signed-in web default.
_LANGUAGES = ("es", "en")

# User lifecycle (ADR-0010). A signup lands 'pending'; the admin moves it
# to 'active' (full access) or 'rejected' (denied).
_USER_STATUSES = ("pending", "active", "rejected")


class OperatorProfile(Base):
    """A human user of the platform (ADR-0010).

    Every downstream row carries an ``operator_id`` foreign key so
    invariants (one open alert per kind, one cookie per gym account)
    stay expressible. Users self-register via OAuth: a new identity
    lands ``status='pending'`` and gains access only when an ``is_admin``
    user approves it (``active``) or is denied (``rejected``).
    """

    __tablename__ = "operator_profile"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    # Email from the OAuth identity; used for email notifications. Nullable
    # because older rows predate capture and GitHub may not expose one.
    email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    # Lifecycle state (ADR-0010). Defaults to 'active' so approval and the
    # bootstrap CLI need not set it; the signup path sets 'pending' itself.
    status: Mapped[str] = mapped_column(
        Enum(*_USER_STATUSES, name="user_status_enum", native_enum=True),
        nullable=False,
        server_default="active",
    )
    # Only an admin can approve or reject other users' signups.
    is_admin: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.false())
    # Ban state (ADR-0010). Null = not banned; a future instant = banned until
    # then (timed); the far-future sentinel = indefinite reversible ban.
    banned_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Optional shorter label the operator sets; falls back to display_name.
    short_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    # Provider avatar URL or a private-blob object path; null renders the
    # neutral placeholder (User Profile FR-008).
    profile_picture_ref: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Governs Telegram rendering and the signed-in web default (ADR-0008).
    communication_language: Mapped[str] = mapped_column(
        Enum(*_LANGUAGES, name="language_enum", native_enum=True),
        nullable=False,
        server_default="en",
    )
    # Optional until the operator binds Telegram via /start (US-007).
    telegram_chat_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class GymAccount(Base):
    """One WodBuster gym membership owned by a user (ADR-0007).

    Multi-gym support (ADR-0007 Decision 1A): every booking-scoped row
    references a ``gym_account`` rather than the user directly. The
    account carries the gym subdomain (``gym_slug``), the per-gym
    operator identifier (``idu``), and the display label. A user holds
    at most one account per gym (``UNIQUE (user_id, gym_slug)``); the
    ``user_id`` FK is the multi-user seam.
    """

    __tablename__ = "gym_account"
    __table_args__ = (UniqueConstraint("user_id", "gym_slug", name="uq_gym_account_user_slug"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    gym_slug: Mapped[str] = mapped_column(String(100), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    idu: Mapped[str] = mapped_column(String(64), nullable=False)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.true())
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
        ForeignKey("operator_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        Enum(*_PROVIDERS, name="provider_enum", native_enum=True), nullable=False
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
    """Recurring weekly booking intent (FR-002).

    Model v2 (2026-07-09): folded the primary class fields onto the
    rule row itself and replaced the multi-preference ``class_preference``
    child with an optional single "second shot" pair. Also replaced
    ``window_offset_hours`` (misleading — reservations open at a specific
    clock time, not N hours before class) with the pair
    ``booking_opens_days_before`` + ``booking_opens_at``.

    The rule fires on ``trigger_day`` at ``booking_opens_at`` where
    ``trigger_day = day_of_week - booking_opens_days_before`` (mod 7).
    See :func:`compute_next_window` for the concrete arithmetic.
    """

    __tablename__ = "scheduler_rule"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gym_account_id: Mapped[int] = mapped_column(
        ForeignKey("gym_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # 0 = Monday .. 6 = Sunday. This is the *attendance* day; the
    # rule fires ``booking_opens_days_before`` days earlier.
    day_of_week: Mapped[int] = mapped_column(Integer, nullable=False)

    # Primary target — what the worker tries to book first.
    class_type: Mapped[str] = mapped_column(String(200), nullable=False)
    class_time: Mapped[str] = mapped_column(String(5), nullable=False)  # HH:MM

    # Reservation window arithmetic. The window opens on
    # ``day_of_week - booking_opens_days_before`` (mod 7) at
    # ``booking_opens_at`` in the operator's local time (UTC for now
    # until we add a timezone column).
    booking_opens_days_before: Mapped[int] = mapped_column(Integer, nullable=False)
    booking_opens_at: Mapped[str] = mapped_column(String(5), nullable=False)  # HH:MM

    # Second shot — if the primary class is unavailable at booking time,
    # the worker retries with these fields. Both null when the operator
    # did not fill the alternative section.
    second_shot_class_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    second_shot_class_time: Mapped[str | None] = mapped_column(String(5), nullable=True)

    active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.true())
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class CookieCredential(Base):
    """Encrypted ``.WBAuth`` blob (ADR-0002, ADR-0005, FR-020).

    One row per gym account represents the active cookie. The paste-and-
    validate flow (US-003) upserts on ``gym_account_id``; historic values
    are not retained because the plaintext must never survive rotation.
    """

    __tablename__ = "cookie_credential"
    __table_args__ = (UniqueConstraint("gym_account_id", name="uq_cookie_credential_gym_account"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gym_account_id: Mapped[int] = mapped_column(
        ForeignKey("gym_account.id", ondelete="CASCADE"), nullable=False
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
        Enum(*_COOKIE_PROBE_STATUSES, name="cookie_probe_status_enum", native_enum=True),
        nullable=True,
    )


class BookingOutcome(Base):
    """One row per booking attempt (FR-012, plan sequence diagram)."""

    __tablename__ = "booking_outcome"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gym_account_id: Mapped[int] = mapped_column(
        ForeignKey("gym_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
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
            native_enum=True,
        ),
        nullable=False,
    )
    # 0-based index into the ``class_preference`` walk that produced the
    # granted outcome. Null when the terminal status is not ``granted``.
    granted_fallback_index: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Full response payload retained for post-mortem (FR-012). The
    # WodBuster response is small (< 4 KB per Phase 0); Text is more
    # than enough and keeps the schema portable if we ever need to
    # introspect via psql without json path syntax.
    response_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class VacationWindow(Base):
    """Date range with skip-and-cancel semantics (FR-015)."""

    __tablename__ = "vacation_window"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gym_account_id: Mapped[int] = mapped_column(
        ForeignKey("gym_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Date-only fields modelled as DateTime for uniformity with the
    # other timestamps; the time component is always midnight UTC.
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
    gym_account_id: Mapped[int] = mapped_column(
        ForeignKey("gym_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    probed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    result: Mapped[str] = mapped_column(
        Enum(*_HEARTBEAT_RESULTS, name="heartbeat_result_enum", native_enum=True),
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
    ``(gym_account_id, kind)``. Enforced with a partial unique index; the
    ``postgresql_where`` argument produces valid Postgres DDL.
    """

    __tablename__ = "alert"
    __table_args__ = (
        Index(
            "uq_alert_open_gym_account_kind",
            "gym_account_id",
            "kind",
            unique=True,
            postgresql_where=text("closed_at IS NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gym_account_id: Mapped[int] = mapped_column(
        ForeignKey("gym_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[str] = mapped_column(
        Enum(*_ALERT_KINDS, name="alert_kind_enum", native_enum=True), nullable=False
    )
    # Free-form JSON payload describing the alert. Stored as JSONB so a
    # later GIN index on inner keys is a straight DDL change if the
    # operator UI grows a search feature.
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    first_emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_emitted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class NotificationOutbox(Base):
    """Pending delivery to Telegram or the web banner pool.

    Every state-mutating write that produces an operator-visible signal
    writes an outbox row in the same transaction (plan cross-cutting
    rule). A dispatcher polls this table.
    """

    __tablename__ = "notification_outbox"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("operator_profile.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Optional gym context for the message (ADR-0007, FR-008). Null for
    # user-level notifications; SET NULL on gym-account deletion so the
    # delivery record survives even if the gym account is removed.
    gym_account_id: Mapped[int | None] = mapped_column(
        ForeignKey("gym_account.id", ondelete="SET NULL"), nullable=True
    )
    kind: Mapped[str] = mapped_column(
        Enum(*_NOTIFICATION_KINDS, name="notification_kind_enum", native_enum=True),
        nullable=False,
    )
    # Provider-scoped target: Telegram chat id, or a UI channel label
    # for the banner pool.
    target: Mapped[str] = mapped_column(String(200), nullable=False)
    # Rendered notification body. Stored as JSONB because dispatchers
    # already deal with a small set of structured shapes (Telegram
    # payload, banner payload) and reading them as text-then-json is a
    # trip we would rather not take on every poll.
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    enqueued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    dispatched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))


__all__ = [
    "Alert",
    "Base",
    "BookingOutcome",
    "CookieCredential",
    "FederatedIdentity",
    "GymAccount",
    "HeartbeatReading",
    "NotificationOutbox",
    "OperatorProfile",
    "SchedulerRule",
    "VacationWindow",
]
