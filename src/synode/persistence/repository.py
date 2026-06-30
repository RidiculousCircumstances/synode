from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from synode.persistence.models import (
    ApprovalRecord,
    ArtifactRecord,
    RunEventRecord,
    RunRecord,
    ToolAuditRecord,
    new_id,
)
from synode.schemas import ApprovalStatus, EventType, RunMode, RunResponse, RunStatus, ToolRisk


class Repository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_run(
        self, task: str, model_provider: str, workspace: str | None = None, mode: RunMode = RunMode.GENERAL
    ) -> RunRecord:
        run = RunRecord(
            id=new_id(), task=task, model_provider=model_provider, workspace=workspace, mode=mode.value
        )
        self.session.add(run)
        await self.session.flush()
        await self.add_event(run.id, EventType.RUN_CREATED.value, None, {"task": task})
        return run

    async def get_run(self, run_id: str) -> RunRecord | None:
        return await self.session.get(RunRecord, run_id)

    async def list_events(self, run_id: str, after_id: int = 0) -> list[RunEventRecord]:
        result = await self.session.execute(
            select(RunEventRecord)
            .where(RunEventRecord.run_id == run_id, RunEventRecord.id > after_id)
            .order_by(RunEventRecord.id)
        )
        return list(result.scalars().all())

    async def set_run_status(
        self, run_id: str, status: RunStatus, final_answer: str | None = None, error: str | None = None
    ) -> None:
        run = await self.get_run(run_id)
        if run is None:
            raise LookupError(f"run not found: {run_id}")
        run.status = status.value
        run.updated_at = datetime.now(UTC)
        if final_answer is not None:
            run.final_answer = final_answer
        if error is not None:
            run.error = error
        await self.session.flush()

    async def add_event(
        self, run_id: str, event_type: str, role: str | None, payload: dict[str, Any]
    ) -> RunEventRecord:
        event = RunEventRecord(run_id=run_id, event_type=event_type, role=role, payload=payload)
        self.session.add(event)
        await self.session.flush()
        return event

    async def create_approval(
        self, run_id: str, tool_name: str, action: str, reason: str, payload: dict[str, Any]
    ) -> ApprovalRecord:
        approval = ApprovalRecord(
            id=new_id(), run_id=run_id, tool_name=tool_name, action=action, reason=reason, payload=payload
        )
        self.session.add(approval)
        await self.set_run_status(run_id, RunStatus.WAITING_APPROVAL)
        await self.add_event(
            run_id,
            EventType.APPROVAL_REQUIRED.value,
            None,
            {"approval_id": approval.id, "tool_name": tool_name, "reason": reason},
        )
        await self.session.flush()
        return approval

    async def decide_approval(
        self, approval_id: str, status: ApprovalStatus, decision_reason: str | None = None
    ) -> ApprovalRecord:
        approval = await self.session.get(ApprovalRecord, approval_id)
        if approval is None:
            raise LookupError(f"approval not found: {approval_id}")
        if approval.status != ApprovalStatus.PENDING.value:
            raise ValueError(f"approval already decided: {approval_id}")
        approval.status = status.value
        approval.decision_reason = decision_reason
        approval.decided_at = datetime.now(UTC)
        await self.session.flush()
        return approval

    async def add_tool_audit(
        self,
        run_id: str,
        role: str,
        tool_name: str,
        risk: ToolRisk,
        status: str,
        input_payload: dict[str, Any],
        output_payload: dict[str, Any],
        approval_id: str | None = None,
    ) -> ToolAuditRecord:
        record = ToolAuditRecord(
            run_id=run_id,
            role=role,
            tool_name=tool_name,
            risk=risk.value,
            status=status,
            input=input_payload,
            output=output_payload,
            approval_id=approval_id,
        )
        self.session.add(record)
        await self.add_event(
            run_id,
            EventType.TOOL_CALLED.value,
            role,
            {"tool_name": tool_name, "risk": risk.value, "status": status, "approval_id": approval_id},
        )
        await self.session.flush()
        return record

    async def add_artifact(
        self, run_id: str, kind: str, content: dict[str, Any], path: str | None = None
    ) -> ArtifactRecord:
        artifact = ArtifactRecord(id=new_id(), run_id=run_id, kind=kind, path=path, content=content)
        self.session.add(artifact)
        await self.session.flush()
        return artifact

    async def get_latest_artifact(self, run_id: str, kind: str) -> ArtifactRecord | None:
        result = await self.session.execute(
            select(ArtifactRecord)
            .where(ArtifactRecord.run_id == run_id, ArtifactRecord.kind == kind)
            .order_by(ArtifactRecord.created_at.desc())
            .limit(1)
        )
        return result.scalars().first()


def to_run_response(run: RunRecord) -> RunResponse:
    return RunResponse(
        id=run.id,
        status=RunStatus(run.status),
        mode=RunMode(run.mode),
        task=run.task,
        workspace=run.workspace,
        model_provider=run.model_provider,
        final_answer=run.final_answer,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )
