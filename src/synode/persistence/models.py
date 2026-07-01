from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from synode.schemas import ApprovalStatus, RunMode, RunStatus, ThreadStatus


def new_id() -> str:
    return str(uuid.uuid4())


class Base(DeclarativeBase):
    pass


class JsonType(JSON):
    """Portable JSON type; migrations use JSONB for Postgres where useful."""


class ThreadRecord(Base):
    __tablename__ = "threads"
    __table_args__ = (Index("ix_threads_status_updated", "status", "updated_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=ThreadStatus.ACTIVE.value, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    runs: Mapped[list["RunRecord"]] = relationship(back_populates="thread")
    messages: Mapped[list["ThreadMessageRecord"]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )


class RunRecord(Base):
    __tablename__ = "runs"
    __table_args__ = (
        Index("ix_runs_thread_created", "thread_id", "created_at"),
        Index("ix_runs_status_created", "status", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="RESTRICT"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default=RunStatus.CREATED.value, nullable=False)
    mode: Mapped[str] = mapped_column(String(32), default=RunMode.GENERAL.value, nullable=False)
    task: Mapped[str] = mapped_column(Text, nullable=False)
    workspace: Mapped[str | None] = mapped_column(Text, nullable=True)
    model_provider: Mapped[str] = mapped_column(String(80), nullable=False)
    default_model_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role_model_profile_ids: Mapped[dict[str, Any]] = mapped_column(
        JsonType().with_variant(JSONB, "postgresql"), default=dict
    )
    agent_graph_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    agent_graph_snapshot: Mapped[dict[str, Any]] = mapped_column(
        JsonType().with_variant(JSONB, "postgresql"), default=dict
    )
    observability_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    final_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    worker_id: Mapped[str | None] = mapped_column(String(120), nullable=True)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    thread: Mapped[ThreadRecord] = relationship(back_populates="runs")
    events: Mapped[list["RunEventRecord"]] = relationship(back_populates="run", cascade="all, delete-orphan")


class ThreadMessageRecord(Base):
    __tablename__ = "thread_messages"
    __table_args__ = (Index("ix_thread_messages_thread_id_id", "thread_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    thread_id: Mapped[str] = mapped_column(ForeignKey("threads.id", ondelete="CASCADE"), nullable=False)
    run_id: Mapped[str | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"), nullable=True)
    author_type: Mapped[str] = mapped_column(String(32), nullable=False)
    author_name: Mapped[str] = mapped_column(String(80), nullable=False)
    message_type: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JsonType().with_variant(JSONB, "postgresql"), default=dict
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    thread: Mapped[ThreadRecord] = relationship(back_populates="messages")


class RunEventRecord(Base):
    __tablename__ = "run_events"
    __table_args__ = (Index("ix_run_events_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    event_type: Mapped[str] = mapped_column(String(80), nullable=False)
    role: Mapped[str | None] = mapped_column(String(80), nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType().with_variant(JSONB, "postgresql"), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    run: Mapped[RunRecord] = relationship(back_populates="events")


class ApprovalRecord(Base):
    __tablename__ = "approvals"
    __table_args__ = (Index("ix_approvals_run_status", "run_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    action: Mapped[str] = mapped_column(String(200), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JsonType().with_variant(JSONB, "postgresql"), default=dict)
    status: Mapped[str] = mapped_column(String(32), default=ApprovalStatus.PENDING.value, nullable=False)
    decision_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolAuditRecord(Base):
    __tablename__ = "tool_audit"
    __table_args__ = (Index("ix_tool_audit_run_id_id", "run_id", "id"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    role: Mapped[str] = mapped_column(String(80), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(200), nullable=False)
    risk: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    input: Mapped[dict[str, Any]] = mapped_column(JsonType().with_variant(JSONB, "postgresql"), default=dict)
    output: Mapped[dict[str, Any]] = mapped_column(JsonType().with_variant(JSONB, "postgresql"), default=dict)
    approval_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ArtifactRecord(Base):
    __tablename__ = "artifacts"
    __table_args__ = (Index("ix_artifacts_run_id_id", "run_id", "id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    run_id: Mapped[str] = mapped_column(ForeignKey("runs.id", ondelete="CASCADE"), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    path: Mapped[str | None] = mapped_column(Text, nullable=True)
    content: Mapped[dict[str, Any]] = mapped_column(JsonType().with_variant(JSONB, "postgresql"), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class MemoryItemRecord(Base):
    __tablename__ = "memory_items"
    __table_args__ = (Index("ix_memory_scope_kind_key", "scope", "kind", "key"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    scope: Mapped[str] = mapped_column(String(120), nullable=False)
    kind: Mapped[str] = mapped_column(String(80), nullable=False)
    key: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[dict[str, Any]] = mapped_column(JsonType().with_variant(JSONB, "postgresql"), default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WorkerHeartbeatRecord(Base):
    __tablename__ = "worker_heartbeats"
    __table_args__ = (Index("ix_worker_heartbeats_heartbeat", "heartbeat_at"),)

    worker_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    hostname: Mapped[str] = mapped_column(String(255), nullable=False)
    pid: Mapped[int] = mapped_column(nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    current_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    heartbeat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class SecretRecord(Base):
    __tablename__ = "secrets"
    __table_args__ = (UniqueConstraint("name", name="uq_secrets_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    encrypted_value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ModelProfileRecord(Base):
    __tablename__ = "model_profiles"
    __table_args__ = (UniqueConstraint("name", name="uq_model_profiles_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    provider_type: Mapped[str] = mapped_column(String(80), nullable=False)
    base_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str] = mapped_column(String(200), nullable=False)
    options: Mapped[dict[str, Any]] = mapped_column(
        JsonType().with_variant(JSONB, "postgresql"), default=dict
    )
    secret_id: Mapped[str | None] = mapped_column(ForeignKey("secrets.id", ondelete="SET NULL"), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentRoleRecord(Base):
    __tablename__ = "agent_roles"
    __table_args__ = (UniqueConstraint("name", name="uq_agent_roles_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    mission: Mapped[str] = mapped_column(Text, nullable=False)
    non_goals: Mapped[list[str]] = mapped_column(JsonType().with_variant(JSONB, "postgresql"), default=list)
    allowed_tools: Mapped[list[str]] = mapped_column(JsonType().with_variant(JSONB, "postgresql"), default=list)
    requires_approval_for: Mapped[list[str]] = mapped_column(
        JsonType().with_variant(JSONB, "postgresql"), default=list
    )
    output_contract: Mapped[str] = mapped_column(Text, nullable=False, default="")
    builtin: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AgentGraphRecord(Base):
    __tablename__ = "agent_graphs"
    __table_args__ = (UniqueConstraint("name", name="uq_agent_graphs_name"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    role_ids: Mapped[list[str]] = mapped_column(JsonType().with_variant(JSONB, "postgresql"), default=list)
    edges: Mapped[list[dict[str, str]]] = mapped_column(
        JsonType().with_variant(JSONB, "postgresql"), default=list
    )
    default_model_profile_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    role_model_profile_ids: Mapped[dict[str, str]] = mapped_column(
        JsonType().with_variant(JSONB, "postgresql"), default=dict
    )
    role_runtime_bindings: Mapped[dict[str, str]] = mapped_column(
        JsonType().with_variant(JSONB, "postgresql"), default=dict
    )
    is_default: Mapped[bool] = mapped_column(Boolean(), default=False, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean(), default=True, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
