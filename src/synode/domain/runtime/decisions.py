from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel, field_validator, model_validator

from synode.domain.models import ToolCall

WORKER_ROLES = frozenset(
    {
        "coder",
        "data_analyst",
        "web_researcher",
        "db_agent",
    }
)


class RiskLevel(StrEnum):
    ANALYSIS = "analysis"
    DOCS = "docs"
    SMALL_CODE = "small-code"
    CRITICAL_CODE = "critical-code"


class ReviewerVerdict(StrEnum):
    PROCEED = "proceed"
    REVISE = "revise"
    BLOCK = "block"


class WorkerPlanStep(BaseModel):
    role: str = Field(min_length=1)
    task: str = Field(min_length=1)
    tool_calls: list[ToolCall] = Field(default_factory=list)


class SupervisorDecision(BaseModel):
    selected_roles: list[str] = Field(min_length=1)
    plan: list[WorkerPlanStep] = Field(min_length=1)
    confidence: Literal["low", "medium", "high"]
    risk_level: RiskLevel
    reasoning_summary: str = Field(min_length=1)

    @field_validator("selected_roles")
    @classmethod
    def selected_roles_must_be_workers(cls, roles: list[str]) -> list[str]:
        system_roles = [role for role in roles if role in {"supervisor", "reviewer"}]
        if system_roles:
            raise ValueError(f"selected_roles must contain worker roles only; got system roles: {system_roles}")
        return roles

    @field_validator("plan")
    @classmethod
    def plan_roles_must_be_selected(cls, plan: list[WorkerPlanStep], info: object) -> list[WorkerPlanStep]:
        data = getattr(info, "data", {})
        selected = set(data.get("selected_roles", []))
        if selected:
            planned = {step.role for step in plan}
            missing = selected - planned
            extra = planned - selected
            if missing or extra:
                raise ValueError(f"plan roles must match selected_roles; missing={missing}, extra={extra}")
        return plan


class ReviewerDecision(BaseModel):
    verdict: ReviewerVerdict
    blockers: list[str] = Field(default_factory=list)
    advisory_risks: list[str] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    required_next_actions: list[str] = Field(default_factory=list)
    confidence: Literal["low", "medium", "high"]


class CodingInspection(BaseModel):
    summary: str = Field(min_length=1)
    relevant_files: list[str] = Field(default_factory=list)
    observed_failures: list[str] = Field(default_factory=list)
    proposed_test_commands: list[list[str]] = Field(default_factory=list)


class FilePatch(BaseModel):
    path: str = Field(min_length=1)
    expected_sha256: str = Field(min_length=64, max_length=64)
    old_text: str
    new_text: str


class PatchProposal(BaseModel):
    action: Literal["patch", "no_change", "needs_operator"] = "patch"
    summary: str = Field(min_length=1)
    patches: list[FilePatch] = Field(default_factory=list)
    verification_commands: list[list[str]] = Field(default_factory=list)
    operator_question: str | None = None

    @model_validator(mode="before")
    @classmethod
    def normalize_operator_question(cls, value: Any) -> Any:
        return _normalize_needs_operator_question(value)

    @model_validator(mode="after")
    def validate_action_payload(self) -> "PatchProposal":
        if self.action == "patch":
            if not self.patches:
                raise ValueError("patch action requires at least one patch")
            if not self.verification_commands:
                raise ValueError("patch action requires at least one verification command")
        elif self.action == "no_change" and not self.verification_commands:
            raise ValueError("no_change action requires at least one verification command")
        elif self.action == "needs_operator" and not (self.operator_question or "").strip():
            raise ValueError("needs_operator action requires operator_question")
        return self


class VerificationPlan(BaseModel):
    commands: list[list[str]] = Field(min_length=1)
    reason: str = Field(min_length=1)


class _NativeLoopToolCallAction(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    action: Literal["tool_call"]
    summary: str = Field(min_length=1)
    tool_call: ToolCall
    payload: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "Optional compatibility field for local models. tool_call.name and tool_call.arguments are authoritative."
        ),
    )
    name: str | None = Field(default=None, exclude=True)
    tool_name: str | None = Field(default=None, exclude=True)
    tool: str | None = Field(default=None, exclude=True)
    arguments: dict[str, Any] | None = Field(default=None, exclude=True)


class _NativeLoopFinishAction(BaseModel):
    action: Literal["finish"]
    summary: str = Field(min_length=1)
    payload: dict[str, Any] = Field(min_length=1)
    tool_call: ToolCall | None = None
    operator_question: str | None = None


class _NativeLoopNeedsOperatorAction(BaseModel):
    action: Literal["needs_operator"]
    summary: str = Field(min_length=1)
    operator_question: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    tool_call: ToolCall | None = None


NativeLoopActionVariant = Annotated[
    _NativeLoopToolCallAction | _NativeLoopFinishAction | _NativeLoopNeedsOperatorAction,
    Field(discriminator="action"),
]


class NativeLoopAction(RootModel[NativeLoopActionVariant]):
    @model_validator(mode="before")
    @classmethod
    def normalize_operator_question(cls, value: Any) -> Any:
        return _normalize_needs_operator_question(value)

    @property
    def action(self) -> Literal["tool_call", "finish", "needs_operator"]:
        return self.root.action

    @property
    def summary(self) -> str:
        return self.root.summary

    @property
    def tool_call(self) -> ToolCall | None:
        return self.root.tool_call

    @property
    def payload(self) -> dict[str, Any]:
        return self.root.payload

    @property
    def operator_question(self) -> str | None:
        return getattr(self.root, "operator_question", None)


def _normalize_needs_operator_question(value: Any) -> Any:
    if not isinstance(value, dict):
        return value
    value = _normalize_tool_call(value)
    value = _normalize_finish_payload(value)
    if value.get("action") != "needs_operator" or str(value.get("operator_question") or "").strip():
        return value
    normalized = dict(value)
    for key in ("question", "prompt", "message", "operator_prompt", "operatorQuestion"):
        text = normalized.get(key)
        if isinstance(text, str) and text.strip():
            normalized["operator_question"] = text
            return normalized
    summary = normalized.get("summary")
    if isinstance(summary, str) and summary.strip():
        normalized["operator_question"] = summary
    return normalized


def _normalize_tool_call(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("action") != "tool_call" or value.get("tool_call"):
        return value
    raw_name = value.get("name") or value.get("tool_name") or value.get("tool")
    payload = value.get("payload")
    if not raw_name and isinstance(payload, dict):
        raw_name = payload.get("name") or payload.get("tool_name") or payload.get("tool")
    if isinstance(value.get("arguments"), dict):
        arguments = dict(value["arguments"])
    elif isinstance(payload, dict) and isinstance(payload.get("arguments"), dict):
        arguments = dict(payload["arguments"])
    elif isinstance(value.get("payload"), dict):
        arguments = dict(value["payload"])
        for key in ("name", "tool_name", "tool", "arguments"):
            arguments.pop(key, None)
    else:
        reserved = {
            "action",
            "summary",
            "tool_call",
            "payload",
            "arguments",
            "name",
            "tool_name",
            "tool",
            "operator_question",
        }
        arguments = {key: item for key, item in value.items() if key not in reserved}
    if not raw_name:
        raw_name = _infer_read_only_tool_name(arguments)
    if not isinstance(raw_name, str) or not raw_name.strip():
        return value
    normalized = dict(value)
    normalized["tool_call"] = {"name": raw_name.strip(), "arguments": arguments}
    return normalized


def _infer_read_only_tool_name(arguments: dict[str, Any]) -> str | None:
    if "pattern" in arguments:
        return "native.fs_search"
    if "glob" in arguments:
        return "native.fs_list"
    return None


def _normalize_finish_payload(value: dict[str, Any]) -> dict[str, Any]:
    if value.get("action") != "finish" or value.get("payload"):
        return value
    payload = value.get("proposal")
    if isinstance(payload, dict) and payload:
        normalized = dict(value)
        normalized["payload"] = payload
        return normalized
    reserved = {"action", "summary", "tool_call", "payload", "operator_question"}
    top_level_payload = {key: item for key, item in value.items() if key not in reserved}
    if top_level_payload:
        normalized = dict(value)
        normalized["payload"] = top_level_payload
        return normalized
    return value
