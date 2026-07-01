"""add graph v2 node contracts

Revision ID: 0008_graph_v2_contracts
Revises: 0007_graph_runtime_bindings
Create Date: 2026-07-01
"""

from __future__ import annotations

import json
import re
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_graph_v2_contracts"
down_revision = "0007_graph_runtime_bindings"
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
        sa.Column("graph_schema_version", sa.Integer(), nullable=False, server_default="2"),
    )
    op.add_column(
        "agent_graphs",
        sa.Column("nodes", _json_type(), nullable=False, server_default=_json_literal("[]")),
    )
    op.add_column(
        "agent_graphs",
        sa.Column("node_edges", _json_type(), nullable=False, server_default=_json_literal("[]")),
    )
    op.add_column(
        "agent_graphs",
        sa.Column("node_runtime_bindings", _json_type(), nullable=False, server_default=_json_literal("{}")),
    )
    op.add_column(
        "agent_graphs",
        sa.Column("node_contracts", _json_type(), nullable=False, server_default=_json_literal("{}")),
    )
    op.create_table(
        "runtime_node_states",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("node_id", sa.String(length=120), nullable=False),
        sa.Column("role", sa.String(length=80), nullable=False),
        sa.Column("backend_id", sa.String(length=80), nullable=False),
        sa.Column("contract_id", sa.String(length=120), nullable=False),
        sa.Column("status", sa.String(length=40), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("external_id", sa.String(length=180), nullable=True),
        sa.Column("approval_id", sa.String(length=36), nullable=True),
        sa.Column("external_state", _json_type(), nullable=False, server_default=_json_literal("{}")),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("approval_forwarded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "node_id", "attempt", name="uq_runtime_node_state_attempt"),
    )
    op.create_index("ix_runtime_node_states_run_id", "runtime_node_states", ["run_id"])
    op.create_index("ix_runtime_node_states_status", "runtime_node_states", ["status"])
    _backfill_graph_nodes()


def downgrade() -> None:
    op.drop_index("ix_runtime_node_states_status", table_name="runtime_node_states")
    op.drop_index("ix_runtime_node_states_run_id", table_name="runtime_node_states")
    op.drop_table("runtime_node_states")
    op.drop_column("agent_graphs", "node_contracts")
    op.drop_column("agent_graphs", "node_runtime_bindings")
    op.drop_column("agent_graphs", "node_edges")
    op.drop_column("agent_graphs", "nodes")
    op.drop_column("agent_graphs", "graph_schema_version")


def _backfill_graph_nodes() -> None:
    connection = op.get_bind()
    graphs = sa.table(
        "agent_graphs",
        sa.column("id", sa.String),
        sa.column("role_ids", _json_type()),
        sa.column("edges", _json_type()),
        sa.column("role_runtime_bindings", _json_type()),
        sa.column("graph_schema_version", sa.Integer),
        sa.column("nodes", _json_type()),
        sa.column("node_edges", _json_type()),
        sa.column("node_runtime_bindings", _json_type()),
        sa.column("node_contracts", _json_type()),
    )
    roles = sa.table("agent_roles", sa.column("id", sa.String), sa.column("name", sa.String))
    role_rows = connection.execute(sa.select(roles.c.id, roles.c.name)).all()
    role_name_by_id = {str(row.id): str(row.name) for row in role_rows}
    role_id_by_name = {name: role_id for role_id, name in role_name_by_id.items()}

    query = sa.select(
        graphs.c.id,
        graphs.c.role_ids,
        graphs.c.edges,
        graphs.c.role_runtime_bindings,
    )
    for graph in connection.execute(query):
        role_ids = _as_list(graph.role_ids)
        edges = _as_list(graph.edges)
        role_bindings = _as_dict(graph.role_runtime_bindings)
        nodes, role_to_node = _nodes_for_roles(role_ids, role_name_by_id)
        node_edges = [
            {"from_node": role_to_node[edge["from_role"]], "to_node": role_to_node[edge["to_role"]]}
            for edge in edges
            if isinstance(edge, dict)
            and edge.get("from_role") in role_to_node
            and edge.get("to_role") in role_to_node
        ]
        node_bindings: dict[str, str] = {}
        for role_key, backend in role_bindings.items():
            role_id = role_key if role_key in role_to_node else role_id_by_name.get(str(role_key))
            node_id = role_to_node.get(str(role_id)) if role_id else None
            if node_id and backend:
                node_bindings[node_id] = str(backend)
        connection.execute(
            graphs.update()
            .where(graphs.c.id == graph.id)
            .values(
                graph_schema_version=2,
                nodes=nodes,
                node_edges=node_edges,
                node_runtime_bindings=node_bindings,
                node_contracts={},
            )
        )


def _nodes_for_roles(role_ids: list[Any], role_name_by_id: dict[str, str]) -> tuple[list[dict[str, str]], dict[str, str]]:
    nodes: list[dict[str, str]] = []
    role_to_node: dict[str, str] = {}
    used: set[str] = set()
    for raw_role_id in role_ids:
        role_id = str(raw_role_id)
        role_name = role_name_by_id.get(role_id, role_id)
        base = _slug(role_name)
        node_id = base
        suffix = 2
        while node_id in used:
            node_id = f"{base}_{suffix}"
            suffix += 1
        used.add(node_id)
        role_to_node[role_id] = node_id
        kind = "control" if role_name in {"supervisor", "reviewer"} else "worker"
        nodes.append({"id": node_id, "role_id": role_id, "label": role_name, "kind": kind})
    return nodes, role_to_node


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9_-]+", "_", value).strip("_").lower()
    return slug or "node"


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, str):
        return json.loads(value)
    return value if isinstance(value, list) else []


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, str):
        return json.loads(value)
    return value if isinstance(value, dict) else {}
