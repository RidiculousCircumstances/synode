from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from synode.schemas import ToolCall

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
    summary: str = Field(min_length=1)
    patches: list[FilePatch] = Field(min_length=1)
    verification_commands: list[list[str]] = Field(min_length=1)


class VerificationPlan(BaseModel):
    commands: list[list[str]] = Field(min_length=1)
    reason: str = Field(min_length=1)
