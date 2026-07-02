"""add agent graph loop policies

Revision ID: 0012_agent_graph_loop_policies
Revises: 0011_operator_requests
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0012_agent_graph_loop_policies"
down_revision = "0011_operator_requests"
branch_labels = None
depends_on = None


def _json_type() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _json_literal(value: str) -> sa.TextClause:
    dialect = op.get_bind().dialect.name
    return sa.text(f"'{value}'::jsonb" if dialect == "postgresql" else f"'{value}'")


def upgrade() -> None:
    op.add_column(
        "agent_graphs",
        sa.Column("node_loop_policies", _json_type(), nullable=False, server_default=_json_literal("{}")),
    )


def downgrade() -> None:
    op.drop_column("agent_graphs", "node_loop_policies")
