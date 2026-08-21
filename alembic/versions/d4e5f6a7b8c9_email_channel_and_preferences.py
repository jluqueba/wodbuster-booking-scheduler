"""Email notifications: add ``email`` kind + ``email_preferences`` (ADR-0011)

Adds the ``email`` value to ``notification_kind_enum`` so the outbox can
carry email rows alongside Telegram and banner, and adds a JSONB
``email_preferences`` column to ``operator_profile`` holding the
per-type toggles (``bookings``, ``session_alerts``). Signup lifecycle
(``account``) mail is transactional and not represented here.

Postgres 12+ supports ``ALTER TYPE ... ADD VALUE`` inside a transaction;
the new value is not used in this migration, only declared.

Revision ID: d4e5f6a7b8c9
Revises: f2b3c4d5e6a7
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "d4e5f6a7b8c9"
down_revision: str | Sequence[str] | None = "f2b3c4d5e6a7"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``IF NOT EXISTS`` keeps the enum change idempotent on a partially
    # migrated database.
    op.execute("ALTER TYPE notification_kind_enum ADD VALUE IF NOT EXISTS 'email'")
    op.add_column(
        "operator_profile",
        sa.Column(
            "email_preferences",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text('\'{"bookings": true, "session_alerts": true}\'::jsonb'),
        ),
    )


def downgrade() -> None:
    op.drop_column("operator_profile", "email_preferences")
    # Postgres cannot remove an enum value in place; the added 'email'
    # value is left as a harmless unused member (see 4e2b9c1a7d5f).
