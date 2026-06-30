from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator


def now_utc() -> datetime:
    return datetime.now(UTC)


class RunStatus(StrEnum):
    CREATED = "created"
    QUEUED = "queued"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    CANCELLING = "cancelling"
    COMPLETED = "completed"
    FAILED = "failed"
    FAILED_VERIFICATION = "failed_verification"
    CANCELLED = "cancelled"


class RunMode(StrEnum):
    GENERAL = "general"
    CODING = "coding"


class ModelProviderType(StrEnum):
    FAKE = "fake"
    OLLAMA = "ollama"
    OPENAI_COMPATIBLE = "openai_compatible"


class ThreadStatus(StrEnum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class ThreadMessageAuthorType(StrEnum):
    USER = "user"
    AGENT = "agent"
    SYSTEM = "system"


class ThreadMessageType(StrEnum):
    TEXT = "text"
    RUN_SUMMARY = "run_summary"
    APPROVAL_REQUEST = "approval_request"
    APPROVAL_DECISION = "approval_decision"
    FINAL = "final"


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
    RUN_QUEUED = "run_queued"
    RUN_STARTED = "run_started"
    INTAKE_COMPLETED = "intake_completed"
    NODE_STARTED = "node_started"
    NODE_COMPLETED = "node_completed"
    ROLE_SELECTED = "role_selected"
    MODEL_INVOKED = "model_invoked"
    MODEL_STREAM_STARTED = "model_stream_started"
    MODEL_TOKEN_DELTA = "model_token_delta"
    MODEL_STREAM_COMPLETED = "model_stream_completed"
    TOOL_CALLED = "tool_called"
    APPROVAL_REQUIRED = "approval_required"
    APPROVAL_DECIDED = "approval_decided"
    ARTIFACT_CREATED = "artifact_created"
    VERIFICATION_COMPLETED = "verification_completed"
    RUN_CANCELLING = "run_cancelling"
    RUN_COMPLETED = "run_completed"
    RUN_FAILED = "run_failed"
    RUN_CANCELLED = "run_cancelled"
    WORKER_HEARTBEAT = "worker_heartbeat"


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


class SecretCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    value: str = Field(min_length=1)

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _non_blank(value, "name")


class SecretUpdateRequest(BaseModel):
    value: str = Field(min_length=1)

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: str) -> str:
        return _non_blank(value, "value")


class SecretResponse(BaseModel):
    id: str
    name: str
    secret_set: bool
    created_at: datetime
    updated_at: datetime


class ModelProfileCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    provider_type: ModelProviderType
    base_url: str | None = None
    model: str = Field(min_length=1)
    options: dict[str, Any] = Field(default_factory=dict)
    secret_id: str | None = None
    enabled: bool = True

    @field_validator("name", "model")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _non_blank(value, "field")


class ModelProfileUpdateRequest(BaseModel):
    name: str | None = None
    provider_type: ModelProviderType | None = None
    base_url: str | None = None
    model: str | None = None
    options: dict[str, Any] | None = None
    secret_id: str | None = None
    enabled: bool | None = None


class ModelProfileResponse(BaseModel):
    id: str
    name: str
    provider_type: ModelProviderType
    base_url: str | None = None
    model: str
    options: dict[str, Any] = Field(default_factory=dict)
    secret_id: str | None = None
    secret_set: bool = False
    enabled: bool
    created_at: datetime
    updated_at: datetime


class AgentRoleCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    mission: str = Field(min_length=1)
    non_goals: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    requires_approval_for: list[str] = Field(default_factory=list)
    output_contract: str = ""
    enabled: bool = True

    @field_validator("name", "mission")
    @classmethod
    def validate_required_text(cls, value: str) -> str:
        return _non_blank(value, "field")


class AgentRoleUpdateRequest(BaseModel):
    mission: str | None = None
    non_goals: list[str] | None = None
    allowed_tools: list[str] | None = None
    requires_approval_for: list[str] | None = None
    output_contract: str | None = None
    enabled: bool | None = None


class AgentRoleResponse(BaseModel):
    id: str
    name: str
    mission: str
    non_goals: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    requires_approval_for: list[str] = Field(default_factory=list)
    output_contract: str
    builtin: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime


class AgentGraphEdge(BaseModel):
    from_role: str = Field(min_length=1)
    to_role: str = Field(min_length=1)


class AgentGraphCreateRequest(BaseModel):
    name: str = Field(min_length=1)
    role_ids: list[str] = Field(min_length=1)
    edges: list[AgentGraphEdge] = Field(default_factory=list)
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] = Field(default_factory=dict)
    is_default: bool = False
    enabled: bool = True

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return _non_blank(value, "name")


class AgentGraphUpdateRequest(BaseModel):
    name: str | None = None
    role_ids: list[str] | None = None
    edges: list[AgentGraphEdge] | None = None
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] | None = None
    is_default: bool | None = None
    enabled: bool | None = None


class AgentGraphResponse(BaseModel):
    id: str
    name: str
    role_ids: list[str] = Field(default_factory=list)
    edges: list[AgentGraphEdge] = Field(default_factory=list)
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] = Field(default_factory=dict)
    is_default: bool
    enabled: bool
    created_at: datetime
    updated_at: datetime


class RunCreateRequest(BaseModel):
    task: str = Field(min_length=1)
    workspace: str | None = None
    model_provider: str | None = None
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] = Field(default_factory=dict)
    agent_graph_id: str | None = None
    mode: RunMode = RunMode.GENERAL

    @field_validator("task")
    @classmethod
    def validate_task(cls, value: str) -> str:
        return _non_blank(value, "task")


class ThreadCreateRequest(BaseModel):
    message: str = Field(min_length=1)
    title: str | None = None
    workspace: str | None = None
    model_provider: str | None = None
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] = Field(default_factory=dict)
    agent_graph_id: str | None = None
    mode: RunMode = RunMode.GENERAL

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _non_blank(value, "message")

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str | None) -> str | None:
        return _optional_non_blank(value, "title")


class ThreadUpdateRequest(BaseModel):
    title: str = Field(min_length=1)

    @field_validator("title")
    @classmethod
    def validate_title(cls, value: str) -> str:
        return _non_blank(value, "title")


class ThreadRunCreateRequest(BaseModel):
    message: str = Field(min_length=1)
    workspace: str | None = None
    model_provider: str | None = None
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] = Field(default_factory=dict)
    agent_graph_id: str | None = None
    mode: RunMode = RunMode.GENERAL

    @field_validator("message")
    @classmethod
    def validate_message(cls, value: str) -> str:
        return _non_blank(value, "message")


class RunResponse(BaseModel):
    id: str
    thread_id: str
    status: RunStatus
    mode: RunMode
    task: str
    workspace: str | None = None
    model_provider: str
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] = Field(default_factory=dict)
    agent_graph_id: str | None = None
    agent_graph_snapshot: dict[str, Any] = Field(default_factory=dict)
    observability_trace_id: str | None = None
    final_answer: str | None = None
    error: str | None = None
    worker_id: str | None = None
    queued_at: datetime | None = None
    started_at: datetime | None = None
    completed_at: datetime | None = None
    heartbeat_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ThreadResponse(BaseModel):
    id: str
    title: str
    status: ThreadStatus
    latest_run_id: str | None = None
    latest_run_status: RunStatus | None = None
    last_message: str | None = None
    created_at: datetime
    updated_at: datetime


class ThreadMessageResponse(BaseModel):
    id: int
    thread_id: str
    run_id: str | None = None
    author_type: ThreadMessageAuthorType
    author_name: str
    message_type: ThreadMessageType
    content: str
    metadata: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime


class ThreadDetailResponse(BaseModel):
    thread: ThreadResponse
    runs: list[RunResponse] = Field(default_factory=list)
    messages: list[ThreadMessageResponse] = Field(default_factory=list)


class ApprovalDecision(BaseModel):
    reason: str | None = None


class RunStopRequest(BaseModel):
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


class SandboxStatusResponse(BaseModel):
    backend: str
    available: bool
    detail: str | None = None
    cpu_seconds: int
    memory_mb: int
    disk_mb: int
    output_max_bytes: int


class WorkerHeartbeatResponse(BaseModel):
    worker_id: str
    hostname: str
    pid: int
    status: str
    current_run_id: str | None = None
    started_at: datetime
    heartbeat_at: datetime


class RuntimeStatusResponse(BaseModel):
    queue_depth: int
    running_count: int
    cancelling_count: int
    stale_running_count: int
    workers: list[WorkerHeartbeatResponse] = Field(default_factory=list)
    sandbox: SandboxStatusResponse


class RetentionCleanupResponse(BaseModel):
    run_events_deleted: int = 0
    model_deltas_deleted: int = 0
    tool_audit_deleted: int = 0
    artifacts_deleted: int = 0
    archived_threads_deleted: int = 0
    runs_deleted: int = 0


def _non_blank(value: str, field_name: str) -> str:
    if not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _optional_non_blank(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _non_blank(value, field_name)
