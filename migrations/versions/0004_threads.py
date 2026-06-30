"""add conversational threads

Revision ID: 0004_threads
Revises: 0003_observability_trace_id
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0004_threads"
down_revision = "0003_observability_trace_id"
branch_labels = None
depends_on = None


def _json_type() -> sa.TypeEngine[object]:
    return sa.JSON().with_variant(postgresql.JSONB(), "postgresql")


def _empty_json_literal() -> str:
    dialect = op.get_bind().dialect.name
    return "'{}'::jsonb" if dialect == "postgresql" else "'{}'"


def upgrade() -> None:
    op.create_table(
        "threads",
        sa.Column("id", sa.String(length=36), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_threads_status_updated", "threads", ["status", "updated_at"])
    op.add_column("runs", sa.Column("thread_id", sa.String(length=36), nullable=True))

    op.execute(
        """
        INSERT INTO threads (id, title, status, created_at, updated_at)
        SELECT
            runs.id,
            COALESCE(NULLIF(left(runs.task, 120), ''), 'Untitled thread'),
            'active',
            runs.created_at,
            runs.updated_at
        FROM runs
        """
    )
    op.execute("UPDATE runs SET thread_id = id WHERE thread_id IS NULL")

    op.alter_column("runs", "thread_id", nullable=False)
    op.create_foreign_key(
        "fk_runs_thread_id_threads",
        "runs",
        "threads",
        ["thread_id"],
        ["id"],
        ondelete="RESTRICT",
    )
    op.create_index("ix_runs_thread_created", "runs", ["thread_id", "created_at"])
    op.create_table(
        "thread_messages",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("thread_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("author_type", sa.String(length=32), nullable=False),
        sa.Column("author_name", sa.String(length=80), nullable=False),
        sa.Column("message_type", sa.String(length=32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("metadata", _json_type(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.ForeignKeyConstraint(["thread_id"], ["threads.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["run_id"], ["runs.id"], ondelete="SET NULL"),
    )
    op.create_index("ix_thread_messages_thread_id_id", "thread_messages", ["thread_id", "id"])
    empty_json = _empty_json_literal()
    op.execute(
        f"""
        INSERT INTO thread_messages (
            thread_id, run_id, author_type, author_name, message_type, content, metadata, created_at
        )
        SELECT id, id, 'user', 'user', 'text', task, {empty_json}, created_at
        FROM runs
        """
    )
    op.execute(
        f"""
        INSERT INTO thread_messages (
            thread_id, run_id, author_type, author_name, message_type, content, metadata, created_at
        )
        SELECT id, id, 'agent', 'final', 'final', final_answer, {empty_json}, updated_at
        FROM runs
        WHERE final_answer IS NOT NULL AND final_answer <> ''
        """
    )


def downgrade() -> None:
    op.drop_index("ix_thread_messages_thread_id_id", table_name="thread_messages")
    op.drop_table("thread_messages")
    op.drop_index("ix_runs_thread_created", table_name="runs")
    op.drop_constraint("fk_runs_thread_id_threads", "runs", type_="foreignkey")
    op.drop_column("runs", "thread_id")
    op.drop_index("ix_threads_status_updated", table_name="threads")
    op.drop_table("threads")
