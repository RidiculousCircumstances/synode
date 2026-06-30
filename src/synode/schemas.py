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
    ROLE_SELECTED = "role_selected"
    TOOL_CALLED = "tool_called"
    APPROVAL_REQUIRED = "approval_required"
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


class RunResponse(BaseModel):
    id: str
    status: RunStatus
    task: str
    workspace: str | None = None
    model_provider: str
    final_answer: str | None = None
    created_at: datetime
    updated_at: datetime


class ApprovalDecision(BaseModel):
    reason: str | None = None

