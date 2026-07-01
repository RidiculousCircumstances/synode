from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class PlanReportStep(BaseModel):
    role: str
    task: str
    status: Literal["planned", "running", "completed", "blocked"] = "planned"
    tool_count: int = 0


class RoleOutputReport(BaseModel):
    role: str
    summary: str
    tool_count: int = 0
    failed_tool_count: int = 0
    risks: list[str] = Field(default_factory=list)


class PatchFileReport(BaseModel):
    path: str
    operation: str = "modified"
    status: Literal["ok", "failed", "pending_approval", "skipped"] = "ok"
    summary: str | None = None
    error: str | None = None


class PatchResultsReport(BaseModel):
    status: Literal["not_applicable", "ok", "failed", "pending_approval", "no_change"] = "not_applicable"
    files: list[PatchFileReport] = Field(default_factory=list)
    raw_count: int = 0


class VerificationCommandReport(BaseModel):
    command: str
    status: Literal["passed", "failed", "skipped", "unknown"] = "unknown"
    summary: str | None = None


class VerificationReport(BaseModel):
    status: Literal["not_run", "passed", "failed", "skipped"] = "not_run"
    commands: list[VerificationCommandReport] = Field(default_factory=list)
    reason: str | None = None


class ToolActivityReport(BaseModel):
    role: str | None = None
    tool_name: str
    status: str
    risk: str | None = None
    title: str
    target: str | None = None
    approval_id: str | None = None


class RunReport(BaseModel):
    version: int = 1
    run_id: str
    thread_id: str
    mode: str
    status: str
    headline: str
    summary: str
    plan: list[PlanReportStep] = Field(default_factory=list)
    role_outputs: list[RoleOutputReport] = Field(default_factory=list)
    patch_results: PatchResultsReport = Field(default_factory=PatchResultsReport)
    verification: VerificationReport = Field(default_factory=VerificationReport)
    tool_activity: list[ToolActivityReport] = Field(default_factory=list)
    blockers: list[str] = Field(default_factory=list)
    advisory: list[str] = Field(default_factory=list)
    diagnostics: dict[str, Any] = Field(default_factory=dict)
    raw_refs: dict[str, str] = Field(default_factory=dict)

    def chat_text(self) -> str:
        lines = [self.headline]
        if self.summary and self.summary != self.headline:
            lines.append(self.summary)
        if self.plan and not self.role_outputs and self.patch_results.status == "not_applicable":
            lines.append("Plan:")
            lines.extend(f"- {step.role}: {step.task}" for step in self.plan[:6])
        if self.blockers:
            lines.append("Blocked: " + "; ".join(self.blockers[:3]))
        elif self.verification.status in {"passed", "skipped", "failed"}:
            lines.append(f"Verification: {self.verification.status.replace('_', ' ')}")
        if self.patch_results.status not in {"not_applicable", "no_change"}:
            changed = len(self.patch_results.files)
            lines.append(f"Patch: {self.patch_results.status.replace('_', ' ')} ({changed} file{'s' if changed != 1 else ''})")
        return "\n".join(line for line in lines if line)

    def to_artifact(self) -> dict[str, Any]:
        return self.model_dump(mode="json")
