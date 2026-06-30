"""add runtime queue and worker metadata

Revision ID: 0006_runtime_operations
Revises: 0005_configurable_agents
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0006_runtime_operations"
down_revision = "0005_configurable_agents"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("runs", sa.Column("worker_id", sa.String(length=120), nullable=True))
    op.add_column("runs", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("runs", sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_runs_status_created", "runs", ["status", "created_at"])
    op.create_table(
        "worker_heartbeats",
        sa.Column("worker_id", sa.String(length=120), primary_key=True),
        sa.Column("hostname", sa.String(length=255), nullable=False),
        sa.Column("pid", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("current_run_id", sa.String(length=36), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_worker_heartbeats_heartbeat", "worker_heartbeats", ["heartbeat_at"])


def downgrade() -> None:
    op.drop_index("ix_worker_heartbeats_heartbeat", table_name="worker_heartbeats")
    op.drop_table("worker_heartbeats")
    op.drop_index("ix_runs_status_created", table_name="runs")
    op.drop_column("runs", "heartbeat_at")
    op.drop_column("runs", "completed_at")
    op.drop_column("runs", "started_at")
    op.drop_column("runs", "queued_at")
    op.drop_column("runs", "worker_id")
