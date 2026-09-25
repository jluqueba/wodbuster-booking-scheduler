"""attendance statistics: capture ledger, records and two settings columns

Creates the two tables behind the statistics feature (ADR-0013):

- ``attendance_day``, the capture ledger, one row per gym account and
  operator-local calendar day, written whether or not the operator had
  activity. ``class_count`` is what distinguishes "the gym was closed"
  from "I did not go", and ``is_final`` is what makes a finished day
  immutable.
- ``attendance_record``, one row per class instance where the operator
  appears in any of the three upstream athlete lists. Keyed uniquely by
  ``(gym_account_id, wodbuster_class_id)`` so a repeated capture is an
  idempotent upsert and two concurrent page loads cannot duplicate a
  row (ADR-0013, Decision 7).

Adds two configuration columns:

- ``operator_profile.statistics_excluded_weekdays``, NOT NULL with a
  ``'[]'`` server default, so existing rows need no data migration.
- ``gym_account.points_model``, nullable with no default, so the
  migration touches no existing row and NULL keeps meaning "use the
  application defaults".

No value is added to an existing enum in this revision, so unlike
a7c3d9e1f4b2 the downgrade is complete rather than partial.

Revision ID: b8e4f1c7a2d3
Revises: a7c3d9e1f4b2
Create Date: 2026-09-24
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "b8e4f1c7a2d3"
down_revision: str | Sequence[str] | None = "a7c3d9e1f4b2"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ATTENDANCE_STATE_ENUM = postgresql.ENUM(
    "attended",
    "cancelled",
    "no_show",
    name="attendance_state_enum",
    # Created and dropped explicitly below; without this the
    # ``create_table`` DDL would emit a second ``CREATE TYPE``.
    create_type=False,
)


def upgrade() -> None:
    _ATTENDANCE_STATE_ENUM.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "attendance_day",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gym_account_id", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column(
            "captured_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("class_count", sa.Integer(), nullable=False),
        sa.Column("is_final", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(["gym_account_id"], ["gym_account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("gym_account_id", "local_date", name="uq_attendance_day_gym_date"),
    )
    op.create_index(
        op.f("ix_attendance_day_gym_account_id"),
        "attendance_day",
        ["gym_account_id"],
        unique=False,
    )

    op.create_table(
        "attendance_record",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("gym_account_id", sa.Integer(), nullable=False),
        sa.Column("local_date", sa.Date(), nullable=False),
        sa.Column("wodbuster_class_id", sa.Integer(), nullable=False),
        sa.Column("class_name", sa.String(length=100), nullable=False),
        sa.Column("class_type_id", sa.Integer(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("state", _ATTENDANCE_STATE_ENUM, nullable=False),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reservation_type", sa.String(length=50), nullable=True),
        sa.Column("capacity", sa.Integer(), nullable=True),
        sa.Column("occupancy", sa.Integer(), nullable=False),
        sa.Column("ever_full", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.ForeignKeyConstraint(["gym_account_id"], ["gym_account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "gym_account_id",
            "wodbuster_class_id",
            name="uq_attendance_record_gym_class",
        ),
    )
    op.create_index(
        op.f("ix_attendance_record_gym_account_id"),
        "attendance_record",
        ["gym_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_attendance_record_gym_start",
        "attendance_record",
        ["gym_account_id", "start_at"],
        unique=False,
    )

    op.add_column(
        "operator_profile",
        sa.Column(
            "statistics_excluded_weekdays",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.add_column(
        "gym_account",
        sa.Column("points_model", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("gym_account", "points_model")
    op.drop_column("operator_profile", "statistics_excluded_weekdays")
    op.drop_index("ix_attendance_record_gym_start", table_name="attendance_record")
    op.drop_index(op.f("ix_attendance_record_gym_account_id"), table_name="attendance_record")
    op.drop_table("attendance_record")
    op.drop_index(op.f("ix_attendance_day_gym_account_id"), table_name="attendance_day")
    op.drop_table("attendance_day")
    _ATTENDANCE_STATE_ENUM.drop(op.get_bind(), checkfirst=True)
