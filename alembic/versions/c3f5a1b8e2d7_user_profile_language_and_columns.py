"""user profile: language_enum + operator_profile columns (ADR-0008)

Extends ``operator_profile`` for the User Profile feature:

- ``short_name`` (nullable) — optional shorter label; falls back to
  ``display_name`` at render time.
- ``profile_picture_ref`` (nullable) — provider avatar URL or a
  private-blob object path; null renders the neutral placeholder
  (FR-008).
- ``communication_language`` (NOT NULL, default ``en``) — a new
  ``language_enum`` (``es`` / ``en``) that governs Telegram rendering
  and the signed-in web default (ADR-0008).

No ``default_gym_account_id`` column: the 2026-07-29 decision is that
gym choice is always explicit via the web nav switcher, so there is no
persisted default gym.

This revision sits on top of the already-deployed multi-gym schema
(``b7d9f2a4c1e8``); the earlier plan assumed a shared revision, but that
one shipped without the profile columns.

Revision ID: c3f5a1b8e2d7
Revises: b7d9f2a4c1e8
Create Date: 2026-07-29
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "c3f5a1b8e2d7"
down_revision: str | Sequence[str] | None = "b7d9f2a4c1e8"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    # Create the native enum type once, then reference it without
    # re-creating it on the column (create_type=False).
    language_enum = postgresql.ENUM("es", "en", name="language_enum")
    language_enum.create(bind, checkfirst=True)

    op.add_column(
        "operator_profile",
        sa.Column("short_name", sa.String(length=100), nullable=True),
    )
    op.add_column(
        "operator_profile",
        sa.Column("profile_picture_ref", sa.String(length=512), nullable=True),
    )
    # NOT NULL with a server default so existing rows adopt 'en' without a
    # separate backfill step.
    op.add_column(
        "operator_profile",
        sa.Column(
            "communication_language",
            postgresql.ENUM("es", "en", name="language_enum", create_type=False),
            nullable=False,
            server_default="en",
        ),
    )


def downgrade() -> None:
    op.drop_column("operator_profile", "communication_language")
    op.drop_column("operator_profile", "profile_picture_ref")
    op.drop_column("operator_profile", "short_name")
    # Native enum types are not dropped with their columns; do it explicitly
    # so a re-upgrade does not fail with DuplicateObject.
    sa.Enum(name="language_enum").drop(op.get_bind(), checkfirst=True)
