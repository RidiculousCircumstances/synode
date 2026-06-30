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
    ThreadMessageRecord,
    ThreadRecord,
    ToolAuditRecord,
    new_id,
)
from synode.schemas import (
    ApprovalStatus,
    EventType,
    RunMode,
    RunResponse,
    RunStatus,
    ThreadMessageAuthorType,
    ThreadMessageResponse,
    ThreadMessageType,
    ThreadResponse,
    ThreadStatus,
    ToolRisk,
)


class Repository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def create_run(
        self,
        task: str,
        model_provider: str,
        workspace: str | None = None,
        mode: RunMode = RunMode.GENERAL,
        observability_trace_id: str | None = None,
        thread_id: str | None = None,
        record_user_message: bool = True,
    ) -> RunRecord:
        thread = await self.get_thread(thread_id) if thread_id is not None else None
        if thread_id is not None and thread is None:
            raise LookupError(f"thread not found: {thread_id}")
        if thread is None:
            thread = await self.create_thread(title=_thread_title(task))
        run = RunRecord(
            id=new_id(),
            thread_id=thread.id,
            task=task,
            model_provider=model_provider,
            workspace=workspace,
            mode=mode.value,
            observability_trace_id=observability_trace_id,
        )
        self.session.add(run)
        await self.session.flush()
        if record_user_message:
            await self.add_thread_message(
                thread.id,
                author_type=ThreadMessageAuthorType.USER,
                author_name="user",
                message_type=ThreadMessageType.TEXT,
                content=task,
                run_id=run.id,
            )
        await self.add_event(run.id, EventType.RUN_CREATED.value, None, {"task": task})
        return run

    async def create_thread(self, title: str) -> ThreadRecord:
        thread = ThreadRecord(id=new_id(), title=_thread_title(title), status=ThreadStatus.ACTIVE.value)
        self.session.add(thread)
        await self.session.flush()
        return thread

    async def get_thread(self, thread_id: str | None) -> ThreadRecord | None:
        if thread_id is None:
            return None
        return await self.session.get(ThreadRecord, thread_id)

    async def list_threads(
        self,
        status: ThreadStatus | None = ThreadStatus.ACTIVE,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ThreadRecord]:
        query = select(ThreadRecord).order_by(ThreadRecord.updated_at.desc(), ThreadRecord.id.desc())
        if status is not None:
            query = query.where(ThreadRecord.status == status.value)
        if search:
            query = query.where(ThreadRecord.title.ilike(f"%{search}%"))
        result = await self.session.execute(query.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def update_thread_title(self, thread_id: str, title: str) -> ThreadRecord:
        thread = await self.get_thread(thread_id)
        if thread is None:
            raise LookupError(f"thread not found: {thread_id}")
        thread.title = _thread_title(title)
        thread.updated_at = datetime.now(UTC)
        await self.session.flush()
        return thread

    async def archive_thread(self, thread_id: str) -> ThreadRecord:
        thread = await self.get_thread(thread_id)
        if thread is None:
            raise LookupError(f"thread not found: {thread_id}")
        thread.status = ThreadStatus.ARCHIVED.value
        thread.updated_at = datetime.now(UTC)
        await self.session.flush()
        return thread

    async def touch_thread(self, thread_id: str) -> None:
        thread = await self.get_thread(thread_id)
        if thread is None:
            raise LookupError(f"thread not found: {thread_id}")
        thread.updated_at = datetime.now(UTC)
        await self.session.flush()

    async def add_thread_message(
        self,
        thread_id: str,
        author_type: ThreadMessageAuthorType,
        author_name: str,
        message_type: ThreadMessageType,
        content: str,
        run_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> ThreadMessageRecord:
        if not content.strip():
            raise ValueError("thread message content is required")
        message = ThreadMessageRecord(
            thread_id=thread_id,
            run_id=run_id,
            author_type=author_type.value,
            author_name=author_name,
            message_type=message_type.value,
            content=content.strip(),
            metadata_=metadata or {},
        )
        self.session.add(message)
        await self.touch_thread(thread_id)
        await self.session.flush()
        return message

    async def list_thread_messages(self, thread_id: str) -> list[ThreadMessageRecord]:
        result = await self.session.execute(
            select(ThreadMessageRecord)
            .where(ThreadMessageRecord.thread_id == thread_id)
            .order_by(ThreadMessageRecord.id)
        )
        return list(result.scalars().all())

    async def latest_thread_message(self, thread_id: str) -> ThreadMessageRecord | None:
        result = await self.session.execute(
            select(ThreadMessageRecord)
            .where(ThreadMessageRecord.thread_id == thread_id)
            .order_by(ThreadMessageRecord.id.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def list_thread_runs(self, thread_id: str) -> list[RunRecord]:
        result = await self.session.execute(
            select(RunRecord)
            .where(RunRecord.thread_id == thread_id)
            .order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
        )
        return list(result.scalars().all())

    async def latest_thread_run(self, thread_id: str) -> RunRecord | None:
        result = await self.session.execute(
            select(RunRecord)
            .where(RunRecord.thread_id == thread_id)
            .order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def get_run(self, run_id: str) -> RunRecord | None:
        return await self.session.get(RunRecord, run_id)

    async def list_runs(
        self,
        status: RunStatus | None = None,
        mode: RunMode | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunRecord]:
        query = select(RunRecord).order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
        if status is not None:
            query = query.where(RunRecord.status == status.value)
        if mode is not None:
            query = query.where(RunRecord.mode == mode.value)
        result = await self.session.execute(query.limit(limit).offset(offset))
        return list(result.scalars().all())

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
        await self.touch_thread(run.thread_id)
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
        run = await self.get_run(run_id)
        if run is not None:
            await self.add_thread_message(
                run.thread_id,
                author_type=ThreadMessageAuthorType.SYSTEM,
                author_name="approval",
                message_type=ThreadMessageType.APPROVAL_REQUEST,
                content=f"Approval required for {tool_name}: {reason}",
                run_id=run_id,
                metadata={"approval_id": approval.id, "tool_name": tool_name, "action": action},
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
        await self.add_event(
            approval.run_id,
            EventType.APPROVAL_DECIDED.value,
            None,
            {
                "approval_id": approval.id,
                "status": status.value,
                "decision_reason": decision_reason,
            },
        )
        await self.session.flush()
        return approval

    async def list_approvals(
        self, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[ApprovalRecord]:
        query = select(ApprovalRecord).order_by(ApprovalRecord.created_at.desc())
        if run_id is not None:
            query = query.where(ApprovalRecord.run_id == run_id)
        if status is not None:
            query = query.where(ApprovalRecord.status == status.value)
        result = await self.session.execute(query)
        return list(result.scalars().all())

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
        await self.add_event(
            run_id,
            EventType.ARTIFACT_CREATED.value,
            None,
            {"artifact_id": artifact.id, "kind": kind, "path": path},
        )
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

    async def list_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        result = await self.session.execute(
            select(ArtifactRecord)
            .where(ArtifactRecord.run_id == run_id)
            .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
        )
        return list(result.scalars().all())

    async def list_tool_audit(self, run_id: str) -> list[ToolAuditRecord]:
        result = await self.session.execute(
            select(ToolAuditRecord)
            .where(ToolAuditRecord.run_id == run_id)
            .order_by(ToolAuditRecord.id)
        )
        return list(result.scalars().all())


def to_run_response(run: RunRecord) -> RunResponse:
    return RunResponse(
        id=run.id,
        thread_id=run.thread_id,
        status=RunStatus(run.status),
        mode=RunMode(run.mode),
        task=run.task,
        workspace=run.workspace,
        model_provider=run.model_provider,
        observability_trace_id=run.observability_trace_id,
        final_answer=run.final_answer,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def to_thread_message_response(message: ThreadMessageRecord) -> ThreadMessageResponse:
    return ThreadMessageResponse(
        id=message.id,
        thread_id=message.thread_id,
        run_id=message.run_id,
        author_type=ThreadMessageAuthorType(message.author_type),
        author_name=message.author_name,
        message_type=ThreadMessageType(message.message_type),
        content=message.content,
        metadata=message.metadata_,
        created_at=message.created_at,
    )


def to_thread_response(
    thread: ThreadRecord,
    latest_run: RunRecord | None = None,
    latest_message: ThreadMessageRecord | None = None,
) -> ThreadResponse:
    return ThreadResponse(
        id=thread.id,
        title=thread.title,
        status=ThreadStatus(thread.status),
        latest_run_id=latest_run.id if latest_run else None,
        latest_run_status=RunStatus(latest_run.status) if latest_run else None,
        last_message=latest_message.content if latest_message else None,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )


def _thread_title(value: str) -> str:
    title = " ".join(value.strip().split())
    if not title:
        return "Untitled thread"
    return title[:120]
