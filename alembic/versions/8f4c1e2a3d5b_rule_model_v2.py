"""rule model v2: fold class fields into scheduler_rule, drop class_preference

Adds the fields the operator-facing rule form now surfaces (class
type, class time, when the booking window opens, optional second
shot) directly on ``scheduler_rule`` and drops the old
``class_preference`` child table + ``window_offset_hours`` column.

The reservation window is now specified precisely as a (days-before,
opens-at) pair. Old ``window_offset_hours`` values (typically 48) are
backfilled as ``days_before = hours / 24`` and ``opens_at = class_time``,
which yields the same absolute instant when the hours divide evenly by
24. Rules with off-multiple offsets (e.g. 58h) get the closest
day-count and keep ``opens_at`` at the class time; the operator can
edit the row post-migration if needed.

Downgrade is intentionally not supported — this is a schema-forward
change with no realistic rollback path (the operator can regenerate
rules from the UI if we ever need to walk back).

Revision ID: 8f4c1e2a3d5b
Revises: 6af804dc63e6
Create Date: 2026-07-09
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "8f4c1e2a3d5b"
down_revision: str | Sequence[str] | None = "6af804dc63e6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. Add the new columns as nullable so backfill can run.
    op.add_column(
        "scheduler_rule",
        sa.Column("class_type", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "scheduler_rule",
        sa.Column("class_time", sa.String(length=5), nullable=True),
    )
    op.add_column(
        "scheduler_rule",
        sa.Column("booking_opens_days_before", sa.Integer(), nullable=True),
    )
    op.add_column(
        "scheduler_rule",
        sa.Column("booking_opens_at", sa.String(length=5), nullable=True),
    )
    op.add_column(
        "scheduler_rule",
        sa.Column("second_shot_class_type", sa.String(length=200), nullable=True),
    )
    op.add_column(
        "scheduler_rule",
        sa.Column("second_shot_class_time", sa.String(length=5), nullable=True),
    )

    # 2. Backfill primary class fields from the ``order_index=0``
    #    preference (Postgres UPDATE...FROM syntax).
    op.execute("""
        UPDATE scheduler_rule sr
        SET class_type = cp.class_type,
            class_time = cp.target_time_slot
        FROM class_preference cp
        WHERE cp.rule_id = sr.id AND cp.order_index = 0
        """)

    # 3. Backfill the optional second-shot fields from ``order_index=1``.
    #    Preferences with higher indices are dropped when the table
    #    goes away below — the new model only supports one alternative.
    op.execute("""
        UPDATE scheduler_rule sr
        SET second_shot_class_type = cp.class_type,
            second_shot_class_time = cp.target_time_slot
        FROM class_preference cp
        WHERE cp.rule_id = sr.id AND cp.order_index = 1
        """)

    # 4. Derive the window-open pair from the old ``window_offset_hours``:
    #    days = hours / 24 (integer div), opens_at = class_time.
    #    Only correct exactly when the offset is a whole number of days;
    #    operator edits the row later if theirs is not.
    op.execute("""
        UPDATE scheduler_rule
        SET booking_opens_days_before = window_offset_hours / 24,
            booking_opens_at = class_time
        WHERE booking_opens_days_before IS NULL AND class_time IS NOT NULL
        """)

    # 5. Fill any orphan rows (no primary preference on the old table)
    #    with conservative defaults so the NOT NULL enforcement below
    #    does not fail.
    op.execute("""
        UPDATE scheduler_rule
        SET class_type = COALESCE(class_type, 'WOD'),
            class_time = COALESCE(class_time, '21:30'),
            booking_opens_days_before = COALESCE(booking_opens_days_before, 2),
            booking_opens_at = COALESCE(booking_opens_at, '21:30')
        """)

    # 6. Enforce NOT NULL on the required columns now that they are
    #    populated on every row.
    op.alter_column("scheduler_rule", "class_type", nullable=False)
    op.alter_column("scheduler_rule", "class_time", nullable=False)
    op.alter_column("scheduler_rule", "booking_opens_days_before", nullable=False)
    op.alter_column("scheduler_rule", "booking_opens_at", nullable=False)

    # 7. Drop the legacy structure. ``class_preference`` had a unique
    #    index and an FK index that must be dropped explicitly on
    #    Postgres.
    op.drop_index(
        "ix_class_preference_rule_id",
        table_name="class_preference",
    )
    op.drop_constraint(
        "uq_class_preference_rule_order",
        "class_preference",
        type_="unique",
    )
    op.drop_table("class_preference")
    op.drop_column("scheduler_rule", "window_offset_hours")


def downgrade() -> None:
    """Not supported. Schema-forward change; operators regenerate rules."""
    raise NotImplementedError(
        "downgrade of 8f4c1e2a3d5b (rule model v2) is not supported"
    )
