"""multi-gym: add gym_account and re-scope booking tables (ADR-0007)

Introduces the ``gym_account`` entity (ADR-0007 Decision 1A) and moves
every booking-scoped table from ``operator_id`` to ``gym_account_id``.
The change is a single expand/backfill/contract revision:

1. create ``gym_account``;
2. seed exactly one gym account per existing operator, sourcing the
   gym slug + idu from ``WODBUSTER_GYM`` / ``WODBUSTER_IDU`` (the
   single-tenant deployment's env config). A fresh database has no
   operators, so seeding is skipped and the env vars are not required;
3. add ``gym_account_id`` (nullable) to the scoped tables and to
   ``cookie_credential``, plus ``notification_outbox`` (which keeps a
   user reference and gains an optional gym context);
4. back-fill each scoped row's ``gym_account_id`` from the seeded
   account whose ``user_id`` matches the old ``operator_id``;
5. set NOT NULL, swap the ``cookie_credential`` uniqueness and the
   ``alert`` open-unique index onto ``gym_account_id``;
6. drop ``operator_id`` from the scoped tables.

Downgrade re-derives ``operator_id`` from ``gym_account.user_id``. It
is lossless only while each user owns exactly one gym account, so it
ABORTS (SEC-008) if any user owns more than one, rather than merging
rows from different gyms under one operator.

Revision ID: b7d9f2a4c1e8
Revises: 4e2b9c1a7d5f
Create Date: 2026-07-23
"""

from __future__ import annotations

import os
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7d9f2a4c1e8"
down_revision: str | Sequence[str] | None = "4e2b9c1a7d5f"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Booking-scoped tables that swap ``operator_id`` for ``gym_account_id``.
# ``cookie_credential`` is included (it gains the column and a unique key)
# but the alert open-unique index and the cookie unique constraint are
# handled explicitly below.
_SCOPED_TABLES: tuple[str, ...] = (
    "scheduler_rule",
    "booking_outcome",
    "vacation_window",
    "heartbeat_reading",
    "alert",
    "cookie_credential",
)


def upgrade() -> None:
    # 1. New gym_account table.
    op.create_table(
        "gym_account",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("gym_slug", sa.String(length=100), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("idu", sa.String(length=64), nullable=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["operator_profile.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("user_id", "gym_slug", name="uq_gym_account_user_slug"),
    )
    op.create_index(op.f("ix_gym_account_user_id"), "gym_account", ["user_id"], unique=False)

    # 2. Seed one gym account per existing operator. A fresh database has
    #    no operators, so this is a no-op there and the env vars are not
    #    consulted (component tests migrate an empty schema).
    bind = op.get_bind()
    operator_ids = [
        row[0] for row in bind.execute(sa.text("SELECT id FROM operator_profile ORDER BY id"))
    ]
    if operator_ids:
        gym_slug = os.environ.get("WODBUSTER_GYM")
        idu = os.environ.get("WODBUSTER_IDU")
        if not gym_slug or not idu:
            raise RuntimeError(
                "Multi-gym migration: WODBUSTER_GYM and WODBUSTER_IDU must be set to seed "
                "the initial gym account for the existing operator(s)."
            )
        for op_id in operator_ids:
            bind.execute(
                sa.text(
                    "INSERT INTO gym_account "
                    "(user_id, gym_slug, display_name, idu, active, created_at) "
                    "VALUES (:uid, :slug, :name, :idu, true, now())"
                ),
                {"uid": op_id, "slug": gym_slug, "name": gym_slug, "idu": idu},
            )

    # 3. notification_outbox: rename operator_id -> user_id, add gym context.
    op.alter_column("notification_outbox", "operator_id", new_column_name="user_id")
    op.execute(
        "ALTER INDEX ix_notification_outbox_operator_id RENAME TO ix_notification_outbox_user_id"
    )
    op.add_column(
        "notification_outbox",
        sa.Column("gym_account_id", sa.Integer(), nullable=True),
    )
    op.create_index(
        op.f("ix_notification_outbox_gym_account_id"),
        "notification_outbox",
        ["gym_account_id"],
        unique=False,
    )
    op.create_foreign_key(
        "fk_notification_outbox_gym_account",
        "notification_outbox",
        "gym_account",
        ["gym_account_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.execute(
        "UPDATE notification_outbox n SET gym_account_id = ga.id "
        "FROM gym_account ga WHERE ga.user_id = n.user_id"
    )

    # 4. Booking-scoped tables + cookie_credential: add gym_account_id,
    #    back-fill, index, FK, NOT NULL.
    for table in _SCOPED_TABLES:
        op.add_column(table, sa.Column("gym_account_id", sa.Integer(), nullable=True))
        op.execute(
            f"UPDATE {table} t SET gym_account_id = ga.id "
            f"FROM gym_account ga WHERE ga.user_id = t.operator_id"
        )
        op.alter_column(table, "gym_account_id", nullable=False)
        op.create_index(op.f(f"ix_{table}_gym_account_id"), table, ["gym_account_id"], unique=False)
        op.create_foreign_key(
            f"fk_{table}_gym_account",
            table,
            "gym_account",
            ["gym_account_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # 5. Swap the alert open-unique index onto (gym_account_id, kind).
    op.drop_index(
        "uq_alert_open_operator_kind",
        table_name="alert",
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "uq_alert_open_gym_account_kind",
        "alert",
        ["gym_account_id", "kind"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    # 6. Swap the cookie_credential uniqueness onto gym_account_id.
    op.drop_constraint("uq_cookie_credential_operator", "cookie_credential", type_="unique")
    op.create_unique_constraint(
        "uq_cookie_credential_gym_account", "cookie_credential", ["gym_account_id"]
    )

    # 7. Drop operator_id (cascades its baseline FK + ix_*_operator_id index).
    for table in _SCOPED_TABLES:
        op.drop_column(table, "operator_id")


def downgrade() -> None:
    bind = op.get_bind()

    # SEC-008 guard: collapsing gym_account_id back to a single operator_id
    # is lossless only while each user owns exactly one gym account.
    dupes = bind.execute(
        sa.text(
            "SELECT count(*) FROM "
            "(SELECT user_id FROM gym_account GROUP BY user_id HAVING count(*) > 1) d"
        )
    ).scalar()
    if dupes:
        raise RuntimeError(
            "Downgrade aborted: at least one user owns more than one gym account. "
            "Collapsing to a single operator_id would merge rows from different gyms "
            "and corrupt data. Remove the extra gym accounts first."
        )

    # Re-add operator_id, back-fill from gym_account.user_id, index, FK, NOT NULL.
    for table in _SCOPED_TABLES:
        op.add_column(table, sa.Column("operator_id", sa.Integer(), nullable=True))
        op.execute(
            f"UPDATE {table} t SET operator_id = ga.user_id "
            f"FROM gym_account ga WHERE ga.id = t.gym_account_id"
        )
        op.alter_column(table, "operator_id", nullable=False)
        op.create_index(op.f(f"ix_{table}_operator_id"), table, ["operator_id"], unique=False)
        op.create_foreign_key(
            f"fk_{table}_operator",
            table,
            "operator_profile",
            ["operator_id"],
            ["id"],
            ondelete="CASCADE",
        )

    # Swap the cookie_credential uniqueness back onto operator_id.
    op.drop_constraint("uq_cookie_credential_gym_account", "cookie_credential", type_="unique")
    op.create_unique_constraint(
        "uq_cookie_credential_operator", "cookie_credential", ["operator_id"]
    )

    # Swap the alert open-unique index back onto (operator_id, kind).
    op.drop_index(
        "uq_alert_open_gym_account_kind",
        table_name="alert",
        postgresql_where=sa.text("closed_at IS NULL"),
    )
    op.create_index(
        "uq_alert_open_operator_kind",
        "alert",
        ["operator_id", "kind"],
        unique=True,
        postgresql_where=sa.text("closed_at IS NULL"),
    )

    # Drop gym_account_id from the scoped tables (cascades FK + index).
    for table in _SCOPED_TABLES:
        op.drop_column(table, "gym_account_id")

    # notification_outbox: drop gym context, rename user_id -> operator_id.
    op.drop_column("notification_outbox", "gym_account_id")
    op.execute(
        "ALTER INDEX ix_notification_outbox_user_id RENAME TO ix_notification_outbox_operator_id"
    )
    op.alter_column("notification_outbox", "user_id", new_column_name="operator_id")

    # Drop gym_account.
    op.drop_index(op.f("ix_gym_account_user_id"), table_name="gym_account")
    op.drop_table("gym_account")
