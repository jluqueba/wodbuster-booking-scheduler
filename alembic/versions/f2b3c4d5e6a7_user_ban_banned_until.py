"""user ban: banned_until on operator_profile (ADR-0010)

Adds ``banned_until`` (nullable timestamptz) to ``operator_profile`` so the
admin can suspend a user temporarily (a future instant), indefinitely (a
far-future sentinel), or clear it (null = not banned). Removal of a user is a
plain DELETE that cascades via existing foreign keys, so no column is needed
for it.

Revision ID: f2b3c4d5e6a7
Revises: e1a2b3c4d5f6
Create Date: 2026-08-19
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f2b3c4d5e6a7"
down_revision: str | Sequence[str] | None = "e1a2b3c4d5f6"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "operator_profile",
        sa.Column("banned_until", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("operator_profile", "banned_until")
