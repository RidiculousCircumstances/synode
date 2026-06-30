"""add run mode

Revision ID: 0002_run_mode
Revises: 0001_initial
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002_run_mode"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column("mode", sa.String(length=32), nullable=False, server_default="general"),
    )
    op.alter_column("runs", "mode", server_default=None)


def downgrade() -> None:
    op.drop_column("runs", "mode")

