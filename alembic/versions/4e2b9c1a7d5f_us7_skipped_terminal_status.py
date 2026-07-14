"""US-007 vacation mode: add ``skipped`` to booking_terminal_status_enum

Introduces the ``skipped`` terminal for booking outcomes that were
never attempted upstream because the scheduler landed inside an
open vacation window (FR-015, US7.2). Keeping the outcome persisted
(rather than silently dropping the run) preserves the audit trail
the operator sees on the history page.

Postgres 12+ supports ``ALTER TYPE ... ADD VALUE`` inside a
transaction. Nothing else changes in this migration — the existing
``vacation_window`` table shipped with the baseline schema.

Revision ID: 4e2b9c1a7d5f
Revises: 8f4c1e2a3d5b
Create Date: 2026-07-14
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "4e2b9c1a7d5f"
down_revision: str | Sequence[str] | None = "8f4c1e2a3d5b"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # ``IF NOT EXISTS`` keeps the migration idempotent — safe to
    # re-apply on a partially-migrated database (e.g. a rolled-back
    # transaction that already committed the enum change).
    op.execute(
        "ALTER TYPE booking_terminal_status_enum ADD VALUE IF NOT EXISTS 'skipped'"
    )


def downgrade() -> None:
    # Postgres has no in-place ``DROP VALUE`` on enums. Rolling back
    # would require rewriting every affected column to a temporary
    # enum type and back, which is disproportionate to the risk
    # (adding an unused value costs nothing). Documented as one-way
    # like the previous rule-model-v2 migration.
    raise NotImplementedError(
        "downgrade unsupported: postgres cannot remove an enum value in place"
    )
