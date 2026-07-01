"""add MCP server registry and proxy sessions

Revision ID: 0010_mcp_proxy
Revises: 0009_drop_legacy_graph_fields
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0010_mcp_proxy"
down_revision = "0009_drop_legacy_graph_fields"
branch_labels = None
depends_on = None


def _json_type() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def upgrade() -> None:
    op.create_table(
        "mcp_servers",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("transport", sa.String(length=40), nullable=False),
        sa.Column("config", _json_type(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("tools", _json_type(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_mcp_servers_name"),
    )
    op.create_table(
        "mcp_proxy_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("backend_id", sa.String(length=80), nullable=False),
        sa.Column("workspace", sa.Text(), nullable=True),
        sa.Column("allowed_tools", _json_type(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_mcp_proxy_sessions_expires", "mcp_proxy_sessions", ["expires_at"])
    op.create_index("ix_mcp_proxy_sessions_run_node", "mcp_proxy_sessions", ["run_id", "node_id"])


def downgrade() -> None:
    op.drop_index("ix_mcp_proxy_sessions_run_node", table_name="mcp_proxy_sessions")
    op.drop_index("ix_mcp_proxy_sessions_expires", table_name="mcp_proxy_sessions")
    op.drop_table("mcp_proxy_sessions")
    op.drop_table("mcp_servers")
