"""user lifecycle: status, is_admin, email on operator_profile (ADR-0010)

Adds the multi-user access columns to ``operator_profile``:

- ``email`` (nullable) — captured from the OAuth identity for email
  notifications; nullable because older rows predate capture and some
  providers may not expose one.
- ``status`` (NOT NULL, default ``active``) — a new ``user_status_enum``
  (``pending`` / ``active`` / ``rejected``). Defaults to ``active`` so the
  approval and bootstrap paths need not set it; the signup path sets
  ``pending`` explicitly.
- ``is_admin`` (NOT NULL, default ``false``) — only an admin approves or
  rejects other users' signups.

Existing rows adopt ``active`` via the server default and are promoted to
``is_admin = true``: the sole pre-existing operator is the administrator.

Revision ID: e1a2b3c4d5f6
Revises: c3f5a1b8e2d7
Create Date: 2026-08-13
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "e1a2b3c4d5f6"
down_revision: str | Sequence[str] | None = "c3f5a1b8e2d7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    status_enum = postgresql.ENUM("pending", "active", "rejected", name="user_status_enum")
    status_enum.create(bind, checkfirst=True)

    op.add_column(
        "operator_profile",
        sa.Column("email", sa.String(length=320), nullable=True),
    )
    op.add_column(
        "operator_profile",
        sa.Column(
            "status",
            postgresql.ENUM(
                "pending", "active", "rejected", name="user_status_enum", create_type=False
            ),
            nullable=False,
            server_default="active",
        ),
    )
    op.add_column(
        "operator_profile",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default=sa.false()),
    )

    # The sole pre-existing operator is the administrator.
    op.execute("UPDATE operator_profile SET is_admin = true")


def downgrade() -> None:
    op.drop_column("operator_profile", "is_admin")
    op.drop_column("operator_profile", "status")
    op.drop_column("operator_profile", "email")
    # Native enum types are not dropped with their columns; do it explicitly
    # so a re-upgrade does not fail with DuplicateObject.
    sa.Enum(name="user_status_enum").drop(op.get_bind(), checkfirst=True)
