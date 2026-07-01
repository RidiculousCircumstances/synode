"""drop legacy agent graph role fields

Revision ID: 0009_drop_legacy_graph_fields
Revises: 0008_graph_v2_contracts
Create Date: 2026-07-01
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_drop_legacy_graph_fields"
down_revision = "0008_graph_v2_contracts"
branch_labels = None
depends_on = None


def _json_type() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _json_literal(value: str) -> sa.TextClause:
    dialect = op.get_bind().dialect.name
    return sa.text(f"'{value}'::jsonb" if dialect == "postgresql" else f"'{value}'")


def upgrade() -> None:
    op.drop_column("agent_graphs", "role_runtime_bindings")
    op.drop_column("agent_graphs", "edges")
    op.drop_column("agent_graphs", "role_ids")


def downgrade() -> None:
    op.add_column(
        "agent_graphs",
        sa.Column("role_ids", _json_type(), nullable=False, server_default=_json_literal("[]")),
    )
    op.add_column(
        "agent_graphs",
        sa.Column("edges", _json_type(), nullable=False, server_default=_json_literal("[]")),
    )
    op.add_column(
        "agent_graphs",
        sa.Column(
            "role_runtime_bindings",
            _json_type(),
            nullable=False,
            server_default=_json_literal("{}"),
        ),
    )
