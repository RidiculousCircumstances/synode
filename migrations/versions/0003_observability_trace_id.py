"""add observability trace id

Revision ID: 0003_observability_trace_id
Revises: 0002_run_mode
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0003_observability_trace_id"
down_revision = "0002_run_mode"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("observability_trace_id", sa.String(length=64), nullable=True))


def downgrade() -> None:
    op.drop_column("runs", "observability_trace_id")
