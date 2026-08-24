"""single-day booking override: table + outcome taxonomy (ADR-0012)

Creates ``booking_day_override``, the per-date exception to a weekly
rule, keyed uniquely by ``(rule_id, target_date)`` and cascading from
both ``scheduler_rule`` and ``gym_account``.

Adds the orthogonal ``booking_outcome.outcome_source`` column
(``rule`` | ``override`` | ``override_fallback`` | ``override_skip``)
so the new terminal results stay distinguishable without touching the
``terminal_status`` vocabulary, and adds ``booking_fallback`` to
``alert_kind_enum`` for the dashboard banner.

Existing ``booking_outcome`` rows are covered by the ``rule`` server
default, so no data migration step is needed.

Postgres 12+ supports ``ALTER TYPE ... ADD VALUE`` inside a
transaction; the new alert value is declared here and used by no
statement in this revision.

Revision ID: a7c3d9e1f4b2
Revises: d4e5f6a7b8c9
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "a7c3d9e1f4b2"
down_revision: str | Sequence[str] | None = "d4e5f6a7b8c9"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OUTCOME_SOURCE_ENUM = postgresql.ENUM(
    "rule",
    "override",
    "override_fallback",
    "override_skip",
    name="booking_outcome_source_enum",
    # The type is created and dropped explicitly below; without this the
    # ``add_column`` DDL would emit a second ``CREATE TYPE``.
    create_type=False,
)


def upgrade() -> None:
    op.create_table(
        "booking_day_override",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("rule_id", sa.Integer(), nullable=False),
        sa.Column("gym_account_id", sa.Integer(), nullable=False),
        sa.Column("target_date", sa.Date(), nullable=False),
        sa.Column("class_type", sa.String(length=200), nullable=True),
        sa.Column("class_time", sa.String(length=5), nullable=True),
        sa.Column("skip_day", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("validated", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "suppress_second_shot",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["rule_id"], ["scheduler_rule.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["gym_account_id"], ["gym_account.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("rule_id", "target_date", name="uq_booking_day_override_rule_date"),
        sa.CheckConstraint(
            "NOT skip_day OR (class_type IS NULL AND class_time IS NULL)",
            name="ck_booking_day_override_skip_exclusive",
        ),
        sa.CheckConstraint(
            "skip_day OR class_type IS NOT NULL OR class_time IS NOT NULL OR suppress_second_shot",
            name="ck_booking_day_override_has_change",
        ),
    )
    op.create_index(
        op.f("ix_booking_day_override_rule_id"),
        "booking_day_override",
        ["rule_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_booking_day_override_gym_account_id"),
        "booking_day_override",
        ["gym_account_id"],
        unique=False,
    )
    op.create_index(
        "ix_booking_day_override_gym_date",
        "booking_day_override",
        ["gym_account_id", "target_date"],
        unique=False,
    )

    _OUTCOME_SOURCE_ENUM.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "booking_outcome",
        sa.Column(
            "outcome_source",
            _OUTCOME_SOURCE_ENUM,
            nullable=False,
            server_default="rule",
        ),
    )

    # Must stay last: no statement in this revision may reference the
    # value added here.
    op.execute("ALTER TYPE alert_kind_enum ADD VALUE IF NOT EXISTS 'booking_fallback'")


def downgrade() -> None:
    op.drop_index("ix_booking_day_override_gym_date", table_name="booking_day_override")
    op.drop_index(op.f("ix_booking_day_override_gym_account_id"), table_name="booking_day_override")
    op.drop_index(op.f("ix_booking_day_override_rule_id"), table_name="booking_day_override")
    op.drop_table("booking_day_override")
    op.drop_column("booking_outcome", "outcome_source")
    _OUTCOME_SOURCE_ENUM.drop(op.get_bind(), checkfirst=True)
    # Postgres cannot remove an enum value in place; the added
    # 'booking_fallback' alert kind is left as a harmless unused member
    # (see 4e2b9c1a7d5f).
