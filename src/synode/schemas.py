from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


def now_utc() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_VERIFICATION = "failed_verification"


class RunMode(StrEnum):
    GENERAL = "general"
    CODING = "coding"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class ToolRisk(StrEnum):
    READ = "read"
    WRITE = "write"
    NETWORK = "network"
    DESTRUCTIVE = "destructive"


class EventType(StrEnum):
    RUN_CREATED = "run_created"
    RUN_STARTED = "run_started"
    INTAKE_COMPLETED = "intake_completed"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    ROLE_SELECTED = "role_selected"
    MODEL_INVOKED = "model_invoked"
    TOOL_CALLED = "tool_called"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DECIDED = "approval_decided"
    ARTIFACT_CREATED = "artifact_created"
    VERIFICATION_COMPLETED = "verification_completed"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"


class RoleName(StrEnum):
    SUPERVISOR = "supervisor"
    CODER = "coder"
    DATA_ANALYST = "data_analyst"
    WEB_RESEARCHER = "web_researcher"
    DB_AGENT = "db_agent"
    REVIEWER = "reviewer"


class ToolCall(BaseModel):
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class ToolResult(BaseModel):
    tool_name: str
    ok: bool
    risk: ToolRisk = ToolRisk.READ
    output: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None
    approval_id: str | None = None


class AgentOutput(BaseModel):
    role: str
    summary: str
    tool_results: list[ToolResult] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)


class PlanStep(BaseModel):
    role: str
    task: str


class RunCreateRequest(BaseModel):
    task: str
    workspace: str | None = None
    model_provider: str | None = None
    mode: RunMode = RunMode.GENERAL


class RunResponse(BaseModel):
    id: str
    status: RunStatus
    mode: RunMode
    task: str
    workspace: str | None = None
    model_provider: str
    observability_trace_id: str | None = None
    final_answer: str | None = None
    created_at: datetime
    updated_at: datetime


class ApprovalDecision(BaseModel):
    reason: str | None = None


class RunEventResponse(BaseModel):
    id: int
    run_id: str
    event_type: str
    role: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ApprovalResponse(BaseModel):
    id: str
    run_id: str
    tool_name: str
    action: str
    reason: str
    payload: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus
    decision_reason: str | None = None
    created_at: datetime
    decided_at: datetime | None = None


class ToolAuditResponse(BaseModel):
    id: int
    run_id: str
    role: str
    tool_name: str
    risk: ToolRisk
    status: str
    input: dict[str, Any] = Field(default_factory=dict)
    output: dict[str, Any] = Field(default_factory=dict)
    approval_id: str | None = None
    created_at: datetime


class ArtifactResponse(BaseModel):
    id: str
    run_id: str
    kind: str
    path: str | None = None
    content: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class TokenUsage(BaseModel):
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None


class RunMetricsResponse(BaseModel):
    run_id: str
    status: RunStatus
    duration_ms: float | None = None
    event_count: int
    model_call_count: int
    tool_call_count: int
    approval_count: int
    pending_approval_count: int
    failed_tool_call_count: int
    token_usage: TokenUsage
    provider_usage: dict[str, TokenUsage] = Field(default_factory=dict)
    latency_ms_by_role: dict[str, float] = Field(default_factory=dict)


class ProcessMetrics(BaseModel):
    pid: int
    uptime_seconds: float
    cpu_percent: float
    memory_rss_bytes: int
    memory_percent: float


class GpuMetrics(BaseModel):
    available: bool
    name: str | None = None
    utilization_percent: float | None = None
    memory_used_mb: float | None = None
    memory_total_mb: float | None = None
    error: str | None = None


class SystemMetricsResponse(BaseModel):
    process: ProcessMetrics
    gpu: list[GpuMetrics] = Field(default_factory=list)
