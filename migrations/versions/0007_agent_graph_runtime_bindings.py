"""add agent graph runtime bindings

Revision ID: 0007_graph_runtime_bindings
Revises: 0006_runtime_operations
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0007_graph_runtime_bindings"
down_revision = "0006_runtime_operations"
branch_labels = None
depends_on = None


def _json_type() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _empty_json_literal() -> sa.TextClause:
    dialect = op.get_bind().dialect.name
    return sa.text("'{}'::jsonb" if dialect == "postgresql" else "'{}'")


def upgrade() -> None:
    op.add_column(
        "agent_graphs",
        sa.Column("role_runtime_bindings", _json_type(), nullable=False, server_default=_empty_json_literal()),
    )


def downgrade() -> None:
    op.drop_column("agent_graphs", "role_runtime_bindings")
