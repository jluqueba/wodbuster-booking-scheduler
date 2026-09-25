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

from datetime import date, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
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
# Orthogonal to the terminal status (ADR-0012, Decision 4): what drove the
# attempt, not how it ended. 'rule' covers every row written before the
# single-day override feature existed.
_BOOKING_OUTCOME_SOURCES = (
    "rule",
    "override",
    "override_fallback",
    "override_skip",
)
_HEARTBEAT_RESULTS = ("valid", "rejected", "unknown")
# The alert kinds intentionally cover only the vocabularies referenced in
# the plan and spec (cookie expiring, cookie invalid, silent-run
# heartbeat anomaly). New kinds land with the story that emits them.
_ALERT_KINDS = (
    "cookie_expiring",
    "cookie_invalid",
    "heartbeat_anomaly",
    "booking_fallback",
)
_NOTIFICATION_KINDS = ("telegram", "banner", "email")
# What the gym calendar said about the operator for one past class
# (ADR-0013, Decision 3). These are the three upstream athlete lists,
# stored verbatim. The "removed after the class began" category from
# ADR-0015 Decision 5 is deliberately NOT a value here: it is derived at
# read time from 'cancelled' plus a state instant at or after the class
# start, so the stored row keeps saying what WodBuster said and the
# interpretation can change without a migration.
_ATTENDANCE_STATES = ("attended", "cancelled", "no_show")
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
    # Per-type email notification preferences (ADR-0011): a JSONB map of
    # toggleable category -> enabled flag; a missing key reads as enabled.
    # 'account' (signup lifecycle) mail is transactional and always sent, so
    # it is intentionally not a key here.
    email_preferences: Mapped[dict[str, bool]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text('\'{"bookings": true, "session_alerts": true}\'::jsonb'),
    )
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
    # Weekdays that never break a training streak for this user
    # (Attendance Statistics FR-037), as a JSONB list of integers with
    # Monday as 0. Applies to every gym account the user owns: a gym's
    # own closing days need no configuration, because a day on which the
    # gym ran no classes never breaks a streak (ADR-0015, Decision 1).
    statistics_excluded_weekdays: Mapped[list[int]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
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
    # Per-gym override of the points economy used by the statistics
    # estimate (Attendance Statistics FR-038). NULL means "use the
    # application defaults from config". Nullable with no server default
    # so the migration touches no existing row, and so a gym that never
    # published its rules is distinguishable from one that matches the
    # defaults.
    points_model: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
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
    outcome_source: Mapped[str] = mapped_column(
        Enum(
            *_BOOKING_OUTCOME_SOURCES,
            name="booking_outcome_source_enum",
            native_enum=True,
        ),
        nullable=False,
        server_default="rule",
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


class BookingDayOverride(Base):
    """Single-date exception to a weekly rule (ADR-0012, Decision 1A).

    The rule is never mutated: this row replaces the rule's target for
    exactly one operator-local calendar day, and every consumer resolves
    an effective target as "override value if present else rule value".

    ``target_date`` is a ``DATE`` in the operator timezone (Decision 2),
    so a time change never changes the row's identity. There is no
    soft-delete column: revert is a hard delete, and a past row is inert
    because the projection only looks forward.
    """

    __tablename__ = "booking_day_override"
    __table_args__ = (
        UniqueConstraint("rule_id", "target_date", name="uq_booking_day_override_rule_date"),
        CheckConstraint(
            "NOT skip_day OR (class_type IS NULL AND class_time IS NULL)",
            name="ck_booking_day_override_skip_exclusive",
        ),
        CheckConstraint(
            "skip_day OR class_type IS NOT NULL OR class_time IS NOT NULL OR suppress_second_shot",
            name="ck_booking_day_override_has_change",
        ),
        Index("ix_booking_day_override_gym_date", "gym_account_id", "target_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    rule_id: Mapped[int] = mapped_column(
        ForeignKey("scheduler_rule.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized from the rule so scoping (ADR-0007) and the
    # projection's range query do not need a join.
    gym_account_id: Mapped[int] = mapped_column(
        ForeignKey("gym_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    target_date: Mapped[date] = mapped_column(Date, nullable=False)
    # Widths mirror ``SchedulerRule`` exactly so an effective value
    # substitutes without truncation. Null means "keep the rule's value".
    class_type: Mapped[str | None] = mapped_column(String(200), nullable=True)
    class_time: Mapped[str | None] = mapped_column(String(5), nullable=True)
    skip_day: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.false())
    # True only when the combination was confirmed against the published
    # schedule of ``target_date``.
    validated: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.false())
    suppress_second_shot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default=sa.false()
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


class AttendanceDay(Base):
    """Capture ledger: one row per gym account and local calendar day.

    Written whether or not the operator had any activity that day, which
    is what makes three cases distinguishable (ADR-0013, Decision 3 and
    INV-005 of the spec):

    - no row at all: the day has never been read from the gym calendar;
    - a row with ``class_count == 0``: the gym ran nothing that day, so
      the day cannot break a training streak (ADR-0015, Decision 1);
    - a row with ``class_count > 0`` and no attendance record: the gym
      ran classes and the operator attended none of them.

    ``is_final`` is true once the local day has ended. A final day is
    never fetched again, because a finished class never changes. Today
    is captured provisionally and re-read on the next visit, which is
    also what lets a coach's later change correct itself.
    """

    __tablename__ = "attendance_day"
    __table_args__ = (
        UniqueConstraint("gym_account_id", "local_date", name="uq_attendance_day_gym_date"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gym_account_id: Mapped[int] = mapped_column(
        ForeignKey("gym_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Operator-local calendar day, resolved with the same timezone rules
    # the scheduler uses, so a DST transition never duplicates or skips
    # a day.
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    # How many class instances the gym ran that day, across all athletes.
    # Not the operator's count: this is the "was the gym open" signal.
    class_count: Mapped[int] = mapped_column(Integer, nullable=False)
    is_final: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.false())


class AttendanceRecord(Base):
    """One class instance where the operator appears upstream (ADR-0013).

    Only the operator is described. The upstream payload carries every
    athlete's display name, profile link and photograph link for each
    class; none of it is persisted, and this class has no column able to
    hold it (INV-001). The two numbers that survive, ``occupancy`` and
    ``capacity``, are anonymous aggregates.

    ``state_changed_at`` is the upstream ``FechaEstado``. Its meaning
    depends on the state: for ``attended`` it is when the booking was
    made, for ``cancelled`` it is when the operator was removed. It is
    nullable because a row with an unparseable instant still counts in
    the abandonment rate and is merely excluded from the lead time
    bands.
    """

    __tablename__ = "attendance_record"
    __table_args__ = (
        UniqueConstraint(
            "gym_account_id",
            "wodbuster_class_id",
            name="uq_attendance_record_gym_class",
        ),
        Index("ix_attendance_record_gym_start", "gym_account_id", "start_at"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    gym_account_id: Mapped[int] = mapped_column(
        ForeignKey("gym_account.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized from ``start_at`` so the streak walk and the range
    # queries join the ledger on a plain DATE without a per-row
    # timezone conversion.
    local_date: Mapped[date] = mapped_column(Date, nullable=False)
    # The upstream class-instance ``Id``. Stable per class, which is what
    # makes a repeated capture an idempotent upsert rather than a
    # duplicate (ADR-0013, Decision 7).
    wodbuster_class_id: Mapped[int] = mapped_column(Integer, nullable=False)
    # String(100) matches ``BookingOutcome.target_class`` exactly, so
    # origin attribution compares like with like without truncation.
    class_name: Mapped[str] = mapped_column(String(100), nullable=False)
    # Upstream ``IdTipoEntrenamiento``. Preferred over the name for
    # grouping, because it survives a rename at the gym.
    class_type_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    start_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    state: Mapped[str] = mapped_column(
        Enum(*_ATTENDANCE_STATES, name="attendance_state_enum", native_enum=True),
        nullable=False,
    )
    state_changed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Upstream ``TipoReserva`` (for example "Tarifa"). Kept because the
    # published points rules price some reservations differently and we
    # cannot yet tell which.
    reservation_type: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # Upstream ``Plazas``. Nullable: observed absent on some instances.
    capacity: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Number of athletes in the attending list. A count, never a roster.
    occupancy: Mapped[int] = mapped_column(Integer, nullable=False)
    # Upstream ``AlgunMomentoLlena``. Stored separately from occupancy
    # because occupancy is a snapshot and this is the only reliable
    # answer to "did this class fill up".
    ever_full: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sa.false())


__all__ = [
    "Alert",
    "AttendanceDay",
    "AttendanceRecord",
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
