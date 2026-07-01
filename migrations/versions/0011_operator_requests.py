"""add operator requests and interaction mode

Revision ID: 0011_operator_requests
Revises: 0010_mcp_proxy
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0011_operator_requests"
down_revision = "0010_mcp_proxy"
branch_labels = None
depends_on = None


def _json_type() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.add_column(
        "runs",
        sa.Column(
            "interaction_mode",
            sa.String(length=32),
            server_default="auto",
            nullable=False,
        ),
    )
    op.create_table(
        "operator_requests",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=120), nullable=True),
        sa.Column("role", sa.String(length=80), nullable=True),
        sa.Column("kind", sa.String(length=40), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("context", _json_type(), nullable=False),
        sa.Column("proposed_payload", _json_type(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("response_payload", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancelled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_operator_requests_run_status", "operator_requests", ["run_id", "status"])
    op.create_index("ix_operator_requests_thread_status", "operator_requests", ["thread_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_operator_requests_thread_status", table_name="operator_requests")
    op.drop_index("ix_operator_requests_run_status", table_name="operator_requests")
    op.drop_table("operator_requests")
    op.drop_column("runs", "interaction_mode")
