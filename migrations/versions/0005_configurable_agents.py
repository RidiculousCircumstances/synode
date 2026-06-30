"""add configurable model profiles and agent graphs

Revision ID: 0005_configurable_agents
Revises: 0004_threads
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0005_configurable_agents"
down_revision = "0004_threads"
branch_labels = None
depends_on = None


def _json_type() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _empty_json_literal() -> sa.TextClause:
    dialect = op.get_bind().dialect.name
    return sa.text("'{}'::jsonb" if dialect == "postgresql" else "'{}'")


def _empty_json_array_literal() -> sa.TextClause:
    dialect = op.get_bind().dialect.name
    return sa.text("'[]'::jsonb" if dialect == "postgresql" else "'[]'")


def upgrade() -> None:
    json_type = _json_type()
    empty_json = _empty_json_literal()
    empty_json_array = _empty_json_array_literal()
    op.create_table(
        "secrets",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("encrypted_value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_secrets_name"),
    )
    op.create_table(
        "model_profiles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("provider_type", sa.String(length=80), nullable=False),
        sa.Column("base_url", sa.Text(), nullable=True),
        sa.Column("model", sa.String(length=200), nullable=False),
        sa.Column("options", json_type, nullable=False, server_default=empty_json),
        sa.Column("secret_id", sa.String(length=36), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["secret_id"], ["secrets.id"], ondelete="SET NULL"),
        sa.UniqueConstraint("name", name="uq_model_profiles_name"),
    )
    op.create_table(
        "agent_roles",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=80), nullable=False),
        sa.Column("mission", sa.Text(), nullable=False),
        sa.Column("non_goals", json_type, nullable=False, server_default=empty_json_array),
        sa.Column("allowed_tools", json_type, nullable=False, server_default=empty_json_array),
        sa.Column("requires_approval_for", json_type, nullable=False, server_default=empty_json_array),
        sa.Column("output_contract", sa.Text(), nullable=False),
        sa.Column("builtin", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_agent_roles_name"),
    )
    op.create_table(
        "agent_graphs",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False),
        sa.Column("role_ids", json_type, nullable=False, server_default=empty_json_array),
        sa.Column("edges", json_type, nullable=False, server_default=empty_json_array),
        sa.Column("default_model_profile_id", sa.String(length=36), nullable=True),
        sa.Column("role_model_profile_ids", json_type, nullable=False, server_default=empty_json),
        sa.Column("is_default", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("name", name="uq_agent_graphs_name"),
    )
    op.add_column("runs", sa.Column("default_model_profile_id", sa.String(length=36), nullable=True))
    op.add_column("runs", sa.Column("role_model_profile_ids", json_type, nullable=False, server_default=empty_json))
    op.add_column("runs", sa.Column("agent_graph_id", sa.String(length=36), nullable=True))
    op.add_column("runs", sa.Column("agent_graph_snapshot", json_type, nullable=False, server_default=empty_json))


def downgrade() -> None:
    op.drop_column("runs", "agent_graph_snapshot")
    op.drop_column("runs", "agent_graph_id")
    op.drop_column("runs", "role_model_profile_ids")
    op.drop_column("runs", "default_model_profile_id")
    op.drop_table("agent_graphs")
    op.drop_table("agent_roles")
    op.drop_table("model_profiles")
    op.drop_table("secrets")
