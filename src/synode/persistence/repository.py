from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from synode.persistence.models import (
    AgentGraphRecord,
    AgentRoleRecord,
    ApprovalRecord,
    ArtifactRecord,
    ModelProfileRecord,
    RunEventRecord,
    RunRecord,
    SecretRecord,
    ThreadMessageRecord,
    ThreadRecord,
    ToolAuditRecord,
    WorkerHeartbeatRecord,
    new_id,
)
from synode.schemas import (
    AgentGraphEdge,
    AgentGraphResponse,
    AgentRoleResponse,
    ApprovalStatus,
    EventType,
    ModelProfileResponse,
    ModelProviderType,
    RunMode,
    RunResponse,
    RunStatus,
    SecretResponse,
    ThreadMessageAuthorType,
    ThreadMessageResponse,
    ThreadMessageType,
    ThreadResponse,
    ThreadStatus,
    ToolRisk,
)

MAX_EVENT_PAYLOAD_BYTES = 65536
MAX_TOOL_AUDIT_PAYLOAD_BYTES = 65536
MAX_ARTIFACT_PAYLOAD_BYTES = 262144


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
        default_model_profile_id: str | None = None,
        role_model_profile_ids: dict[str, str] | None = None,
        agent_graph_id: str | None = None,
        agent_graph_snapshot: dict[str, Any] | None = None,
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
            default_model_profile_id=default_model_profile_id,
            role_model_profile_ids=role_model_profile_ids or {},
            agent_graph_id=agent_graph_id,
            agent_graph_snapshot=agent_graph_snapshot or {},
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

    async def list_thread_messages(
        self,
        thread_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ThreadMessageRecord]:
        result = await self.session.execute(
            select(ThreadMessageRecord)
            .where(ThreadMessageRecord.thread_id == thread_id)
            .order_by(ThreadMessageRecord.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def latest_thread_messages(
        self,
        thread_id: str,
        limit: int = 30,
    ) -> list[ThreadMessageRecord]:
        result = await self.session.execute(
            select(ThreadMessageRecord)
            .where(ThreadMessageRecord.thread_id == thread_id)
            .order_by(ThreadMessageRecord.id.desc())
            .limit(limit)
        )
        return list(reversed(result.scalars().all()))

    async def latest_thread_message(self, thread_id: str) -> ThreadMessageRecord | None:
        result = await self.session.execute(
            select(ThreadMessageRecord)
            .where(ThreadMessageRecord.thread_id == thread_id)
            .order_by(ThreadMessageRecord.id.desc())
            .limit(1)
        )
        return result.scalars().first()

    async def list_thread_runs(self, thread_id: str, limit: int = 50, offset: int = 0) -> list[RunRecord]:
        result = await self.session.execute(
            select(RunRecord)
            .where(RunRecord.thread_id == thread_id)
            .order_by(RunRecord.created_at.desc(), RunRecord.id.desc())
            .limit(limit)
            .offset(offset)
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

    async def count_runs(self, status: RunStatus | None = None) -> int:
        query = select(func.count()).select_from(RunRecord)
        if status is not None:
            query = query.where(RunRecord.status == status.value)
        return int(await self.session.scalar(query) or 0)

    async def list_queued_run_ids(self, limit: int = 1000) -> list[str]:
        result = await self.session.execute(
            select(RunRecord.id)
            .where(RunRecord.status == RunStatus.QUEUED.value)
            .order_by(RunRecord.queued_at.asc().nulls_last(), RunRecord.created_at.asc(), RunRecord.id.asc())
            .limit(limit)
        )
        return [str(run_id) for run_id in result.scalars().all()]

    async def count_stale_running_runs(self, stale_before: datetime) -> int:
        return int(
            await self.session.scalar(
                select(func.count())
                .select_from(RunRecord)
                .where(
                    RunRecord.status.in_({RunStatus.RUNNING.value, RunStatus.CANCELLING.value}),
                    or_(
                        RunRecord.heartbeat_at < stale_before,
                        RunRecord.heartbeat_at.is_(None) & (RunRecord.updated_at < stale_before),
                    ),
                )
            )
            or 0
        )

    async def list_events(self, run_id: str, after_id: int = 0, limit: int = 200) -> list[RunEventRecord]:
        result = await self.session.execute(
            select(RunEventRecord)
            .where(RunEventRecord.run_id == run_id, RunEventRecord.id > after_id)
            .order_by(RunEventRecord.id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def count_events(self, run_id: str) -> int:
        return int(
            await self.session.scalar(
                select(func.count()).select_from(RunEventRecord).where(RunEventRecord.run_id == run_id)
            )
            or 0
        )

    async def model_invocation_events(self, run_id: str) -> list[RunEventRecord]:
        result = await self.session.execute(
            select(RunEventRecord)
            .where(RunEventRecord.run_id == run_id, RunEventRecord.event_type == EventType.MODEL_INVOKED.value)
            .order_by(RunEventRecord.id)
        )
        return list(result.scalars().all())

    async def set_run_status(
        self, run_id: str, status: RunStatus, final_answer: str | None = None, error: str | None = None
    ) -> None:
        run = await self.get_run(run_id)
        if run is None:
            raise LookupError(f"run not found: {run_id}")
        now = datetime.now(UTC)
        run.status = status.value
        run.updated_at = now
        if status == RunStatus.QUEUED:
            run.queued_at = now
            run.completed_at = None
        elif status == RunStatus.RUNNING:
            run.started_at = run.started_at or now
            run.heartbeat_at = now
            run.completed_at = None
        elif status in {
            RunStatus.WAITING_APPROVAL,
            RunStatus.COMPLETED,
            RunStatus.FAILED,
            RunStatus.FAILED_VERIFICATION,
            RunStatus.CANCELLED,
        }:
            run.worker_id = None
            run.heartbeat_at = None
            if status in {
                RunStatus.COMPLETED,
                RunStatus.FAILED,
                RunStatus.FAILED_VERIFICATION,
                RunStatus.CANCELLED,
            }:
                run.completed_at = now
        await self.touch_thread(run.thread_id)
        if final_answer is not None:
            run.final_answer = final_answer
        if error is not None:
            run.error = error
        await self.session.flush()

    async def enqueue_run(self, run_id: str) -> RunRecord:
        run = await self.get_run(run_id)
        if run is None:
            raise LookupError(f"run not found: {run_id}")
        if run.status in {
            RunStatus.RUNNING.value,
            RunStatus.CANCELLING.value,
            RunStatus.COMPLETED.value,
            RunStatus.FAILED.value,
            RunStatus.FAILED_VERIFICATION.value,
            RunStatus.CANCELLED.value,
        }:
            raise ValueError(f"run cannot be queued from status {run.status}: {run_id}")
        if run.status == RunStatus.WAITING_APPROVAL.value:
            pending = await self.count_approvals(run_id, status=ApprovalStatus.PENDING)
            if pending:
                raise ValueError(f"run has pending approvals: {run_id}")
        if run.status != RunStatus.QUEUED.value:
            await self.set_run_status(run_id, RunStatus.QUEUED)
            await self.add_event(run_id, EventType.RUN_QUEUED.value, None, {})
        return run

    async def claim_queued_run(self, run_id: str, worker_id: str) -> RunRecord | None:
        query = select(RunRecord).where(
            RunRecord.id == run_id,
            RunRecord.status == RunStatus.QUEUED.value,
        )
        if self.session.bind is not None and self.session.bind.dialect.name == "postgresql":
            query = query.with_for_update(skip_locked=True)
        result = await self.session.execute(query)
        run = result.scalars().first()
        if run is None:
            return None
        now = datetime.now(UTC)
        run.status = RunStatus.RUNNING.value
        run.worker_id = worker_id
        run.started_at = now
        run.heartbeat_at = now
        run.completed_at = None
        run.updated_at = now
        await self.touch_thread(run.thread_id)
        await self.session.flush()
        return run

    async def heartbeat_run(self, run_id: str, worker_id: str) -> None:
        run = await self.get_run(run_id)
        if run is None:
            raise LookupError(f"run not found: {run_id}")
        if run.status not in {RunStatus.RUNNING.value, RunStatus.CANCELLING.value}:
            return
        run.worker_id = worker_id
        run.heartbeat_at = datetime.now(UTC)
        await self.session.flush()

    async def request_run_cancellation(self, run_id: str, reason: str) -> RunRecord:
        run = await self.get_run(run_id)
        if run is None:
            raise LookupError(f"run not found: {run_id}")
        if run.status == RunStatus.CANCELLING.value:
            return run
        run.status = RunStatus.CANCELLING.value
        run.error = reason
        run.updated_at = datetime.now(UTC)
        await self.touch_thread(run.thread_id)
        await self.add_event(run_id, EventType.RUN_CANCELLING.value, None, {"reason": reason})
        await self.add_thread_message(
            run.thread_id,
            author_type=ThreadMessageAuthorType.SYSTEM,
            author_name="runtime",
            message_type=ThreadMessageType.RUN_SUMMARY,
            content=f"Run cancelling: {reason}",
            run_id=run_id,
            metadata={"status": RunStatus.CANCELLING.value},
        )
        await self.session.flush()
        return run

    async def recover_stale_runs(self, stale_before: datetime) -> dict[str, Any]:
        running = await self.session.execute(
            select(RunRecord).where(
                RunRecord.status == RunStatus.RUNNING.value,
                or_(
                    RunRecord.heartbeat_at < stale_before,
                    RunRecord.heartbeat_at.is_(None) & (RunRecord.updated_at < stale_before),
                ),
            )
        )
        cancelling = await self.session.execute(
            select(RunRecord).where(
                RunRecord.status == RunStatus.CANCELLING.value,
                or_(
                    RunRecord.heartbeat_at < stale_before,
                    RunRecord.heartbeat_at.is_(None) & (RunRecord.updated_at < stale_before),
                ),
            )
        )
        recovered = 0
        cancelled = 0
        recovered_run_ids: list[str] = []
        cancelled_run_ids: list[str] = []
        for run in running.scalars().all():
            now = datetime.now(UTC)
            run.status = RunStatus.QUEUED.value
            run.worker_id = None
            run.queued_at = now
            run.heartbeat_at = None
            run.updated_at = now
            run.error = "Recovered stale running run after worker heartbeat expired."
            await self.touch_thread(run.thread_id)
            await self.add_event(
                run.id,
                EventType.RUN_QUEUED.value,
                None,
                {"reason": "stale_worker_recovery"},
            )
            recovered += 1
            recovered_run_ids.append(run.id)
        for run in cancelling.scalars().all():
            run.worker_id = None
            run.heartbeat_at = None
            await self.set_run_status(
                run.id,
                RunStatus.CANCELLED,
                final_answer=run.error or "Run stopped by user.",
                error=run.error or "Run stopped by user.",
            )
            await self.add_event(
                run.id,
                EventType.RUN_CANCELLED.value,
                None,
                {"reason": run.error or "Run stopped by user.", "stale": True},
            )
            cancelled += 1
            cancelled_run_ids.append(run.id)
        await self.session.flush()
        return {
            "requeued": recovered,
            "cancelled": cancelled,
            "requeued_run_ids": recovered_run_ids,
            "cancelled_run_ids": cancelled_run_ids,
        }

    async def add_event(
        self, run_id: str, event_type: str, role: str | None, payload: dict[str, Any]
    ) -> RunEventRecord:
        event = RunEventRecord(
            run_id=run_id,
            event_type=event_type,
            role=role,
            payload=_truncate_json_payload(payload, MAX_EVENT_PAYLOAD_BYTES),
        )
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

    async def reject_pending_approvals(self, run_id: str, decision_reason: str | None = None) -> list[ApprovalRecord]:
        result = await self.session.execute(
            select(ApprovalRecord)
            .where(ApprovalRecord.run_id == run_id, ApprovalRecord.status == ApprovalStatus.PENDING.value)
            .order_by(ApprovalRecord.created_at)
        )
        approvals = list(result.scalars().all())
        for approval in approvals:
            approval.status = ApprovalStatus.REJECTED.value
            approval.decision_reason = decision_reason
            approval.decided_at = datetime.now(UTC)
            await self.add_event(
                approval.run_id,
                EventType.APPROVAL_DECIDED.value,
                None,
                {
                    "approval_id": approval.id,
                    "status": ApprovalStatus.REJECTED.value,
                    "decision_reason": decision_reason,
                },
            )
        await self.session.flush()
        return approvals

    async def list_approvals(
        self,
        run_id: str | None = None,
        status: ApprovalStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ApprovalRecord]:
        query = select(ApprovalRecord).order_by(ApprovalRecord.created_at.desc())
        if run_id is not None:
            query = query.where(ApprovalRecord.run_id == run_id)
        if status is not None:
            query = query.where(ApprovalRecord.status == status.value)
        result = await self.session.execute(query.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def count_approvals(self, run_id: str, status: ApprovalStatus | None = None) -> int:
        query = select(func.count()).select_from(ApprovalRecord).where(ApprovalRecord.run_id == run_id)
        if status is not None:
            query = query.where(ApprovalRecord.status == status.value)
        return int(await self.session.scalar(query) or 0)

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
            input=_truncate_json_payload(input_payload, MAX_TOOL_AUDIT_PAYLOAD_BYTES),
            output=_truncate_json_payload(output_payload, MAX_TOOL_AUDIT_PAYLOAD_BYTES),
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
        artifact = ArtifactRecord(
            id=new_id(),
            run_id=run_id,
            kind=kind,
            path=path,
            content=_truncate_json_payload(content, MAX_ARTIFACT_PAYLOAD_BYTES),
        )
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

    async def list_artifacts(self, run_id: str, limit: int = 100, offset: int = 0) -> list[ArtifactRecord]:
        result = await self.session.execute(
            select(ArtifactRecord)
            .where(ArtifactRecord.run_id == run_id)
            .order_by(ArtifactRecord.created_at.desc(), ArtifactRecord.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def list_tool_audit(self, run_id: str, limit: int = 200, offset: int = 0) -> list[ToolAuditRecord]:
        result = await self.session.execute(
            select(ToolAuditRecord)
            .where(ToolAuditRecord.run_id == run_id)
            .order_by(ToolAuditRecord.id)
            .limit(limit)
            .offset(offset)
        )
        return list(result.scalars().all())

    async def upsert_worker_heartbeat(
        self,
        worker_id: str,
        hostname: str,
        pid: int,
        status: str,
        current_run_id: str | None,
        started_at: datetime,
    ) -> WorkerHeartbeatRecord:
        now = datetime.now(UTC)
        record = await self.session.get(WorkerHeartbeatRecord, worker_id)
        if record is None:
            record = WorkerHeartbeatRecord(
                worker_id=worker_id,
                hostname=hostname,
                pid=pid,
                status=status,
                current_run_id=current_run_id,
                started_at=started_at,
                heartbeat_at=now,
            )
            self.session.add(record)
        else:
            record.hostname = hostname
            record.pid = pid
            record.status = status
            record.current_run_id = current_run_id
            record.heartbeat_at = now
        await self.session.flush()
        return record

    async def list_worker_heartbeats(self, limit: int = 50) -> list[WorkerHeartbeatRecord]:
        result = await self.session.execute(
            select(WorkerHeartbeatRecord)
            .order_by(WorkerHeartbeatRecord.heartbeat_at.desc(), WorkerHeartbeatRecord.worker_id)
            .limit(limit)
        )
        return list(result.scalars().all())

    async def cleanup_retention(
        self,
        *,
        now: datetime | None = None,
        run_event_days: int,
        model_delta_days: int,
        tool_audit_days: int,
        artifact_days: int,
        archived_thread_days: int,
    ) -> dict[str, int]:
        current = now or datetime.now(UTC)
        model_delta_result = await self.session.execute(
            delete(RunEventRecord).where(
                RunEventRecord.event_type == EventType.MODEL_TOKEN_DELTA.value,
                RunEventRecord.created_at < current - timedelta(days=model_delta_days),
            )
        )
        event_result = await self.session.execute(
            delete(RunEventRecord).where(
                RunEventRecord.event_type != EventType.MODEL_TOKEN_DELTA.value,
                RunEventRecord.created_at < current - timedelta(days=run_event_days),
            )
        )
        audit_result = await self.session.execute(
            delete(ToolAuditRecord).where(
                ToolAuditRecord.created_at < current - timedelta(days=tool_audit_days)
            )
        )
        artifact_result = await self.session.execute(
            delete(ArtifactRecord).where(ArtifactRecord.created_at < current - timedelta(days=artifact_days))
        )

        archived_cutoff = current - timedelta(days=archived_thread_days)
        archived_thread_ids = select(ThreadRecord.id).where(
            ThreadRecord.status == ThreadStatus.ARCHIVED.value,
            ThreadRecord.updated_at < archived_cutoff,
        )
        runs_result = await self.session.execute(delete(RunRecord).where(RunRecord.thread_id.in_(archived_thread_ids)))
        threads_result = await self.session.execute(
            delete(ThreadRecord).where(
                ThreadRecord.status == ThreadStatus.ARCHIVED.value,
                ThreadRecord.updated_at < archived_cutoff,
            )
        )
        await self.session.flush()
        return {
            "run_events_deleted": _rowcount(event_result),
            "model_deltas_deleted": _rowcount(model_delta_result),
            "tool_audit_deleted": _rowcount(audit_result),
            "artifacts_deleted": _rowcount(artifact_result),
            "archived_threads_deleted": _rowcount(threads_result),
            "runs_deleted": _rowcount(runs_result),
        }

    async def count_tool_audit(self, run_id: str, statuses: set[str] | None = None) -> int:
        query = select(func.count()).select_from(ToolAuditRecord).where(ToolAuditRecord.run_id == run_id)
        if statuses:
            query = query.where(ToolAuditRecord.status.in_(statuses))
        return int(await self.session.scalar(query) or 0)

    async def create_secret(self, name: str, encrypted_value: str) -> SecretRecord:
        record = SecretRecord(id=new_id(), name=name, encrypted_value=encrypted_value)
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_secret(self, secret_id: str | None) -> SecretRecord | None:
        if secret_id is None:
            return None
        return await self.session.get(SecretRecord, secret_id)

    async def list_secrets(self, limit: int = 50, offset: int = 0) -> list[SecretRecord]:
        result = await self.session.execute(select(SecretRecord).order_by(SecretRecord.name).limit(limit).offset(offset))
        return list(result.scalars().all())

    async def update_secret(self, secret_id: str, encrypted_value: str) -> SecretRecord:
        record = await self.get_secret(secret_id)
        if record is None:
            raise LookupError(f"secret not found: {secret_id}")
        record.encrypted_value = encrypted_value
        record.updated_at = datetime.now(UTC)
        await self.session.flush()
        return record

    async def create_model_profile(
        self,
        name: str,
        provider_type: ModelProviderType,
        model: str,
        base_url: str | None = None,
        options: dict[str, Any] | None = None,
        secret_id: str | None = None,
        enabled: bool = True,
    ) -> ModelProfileRecord:
        if secret_id is not None and await self.get_secret(secret_id) is None:
            raise LookupError(f"secret not found: {secret_id}")
        record = ModelProfileRecord(
            id=new_id(),
            name=name,
            provider_type=provider_type.value,
            base_url=base_url,
            model=model,
            options=options or {},
            secret_id=secret_id,
            enabled=enabled,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_model_profile(self, profile_id: str | None) -> ModelProfileRecord | None:
        if profile_id is None:
            return None
        return await self.session.get(ModelProfileRecord, profile_id)

    async def list_model_profiles(
        self,
        enabled_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ModelProfileRecord]:
        query = select(ModelProfileRecord).order_by(ModelProfileRecord.name)
        if enabled_only:
            query = query.where(ModelProfileRecord.enabled.is_(True))
        result = await self.session.execute(query.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def update_model_profile(self, profile_id: str, values: dict[str, Any]) -> ModelProfileRecord:
        record = await self.get_model_profile(profile_id)
        if record is None:
            raise LookupError(f"model profile not found: {profile_id}")
        if "secret_id" in values and values["secret_id"] is not None and await self.get_secret(values["secret_id"]) is None:
            raise LookupError(f"secret not found: {values['secret_id']}")
        for key, value in values.items():
            if value is not None or key in {"base_url", "secret_id"}:
                if key == "provider_type" and isinstance(value, ModelProviderType):
                    value = value.value
                setattr(record, key, value)
        record.updated_at = datetime.now(UTC)
        await self.session.flush()
        return record

    async def create_agent_role(
        self,
        name: str,
        mission: str,
        non_goals: list[str] | None = None,
        allowed_tools: list[str] | None = None,
        requires_approval_for: list[str] | None = None,
        output_contract: str = "",
        builtin: bool = False,
        enabled: bool = True,
    ) -> AgentRoleRecord:
        record = AgentRoleRecord(
            id=new_id(),
            name=name,
            mission=mission,
            non_goals=non_goals or [],
            allowed_tools=allowed_tools or [],
            requires_approval_for=requires_approval_for or [],
            output_contract=output_contract,
            builtin=builtin,
            enabled=enabled,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_agent_role(self, role_id: str | None) -> AgentRoleRecord | None:
        if role_id is None:
            return None
        return await self.session.get(AgentRoleRecord, role_id)

    async def get_agent_role_by_name(self, name: str) -> AgentRoleRecord | None:
        result = await self.session.execute(select(AgentRoleRecord).where(AgentRoleRecord.name == name))
        return result.scalars().first()

    async def list_agent_roles(
        self,
        enabled_only: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[AgentRoleRecord]:
        query = select(AgentRoleRecord).order_by(AgentRoleRecord.name)
        if enabled_only:
            query = query.where(AgentRoleRecord.enabled.is_(True))
        result = await self.session.execute(query.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def update_agent_role(self, role_id: str, values: dict[str, Any]) -> AgentRoleRecord:
        record = await self.get_agent_role(role_id)
        if record is None:
            raise LookupError(f"agent role not found: {role_id}")
        for key, value in values.items():
            if value is not None:
                setattr(record, key, value)
        record.updated_at = datetime.now(UTC)
        await self.session.flush()
        return record

    async def create_agent_graph(
        self,
        name: str,
        role_ids: list[str],
        edges: list[dict[str, str]],
        default_model_profile_id: str | None = None,
        role_model_profile_ids: dict[str, str] | None = None,
        is_default: bool = False,
        enabled: bool = True,
    ) -> AgentGraphRecord:
        await self._validate_graph_refs(role_ids, edges, default_model_profile_id, role_model_profile_ids or {})
        if is_default:
            await self._clear_default_graphs()
        record = AgentGraphRecord(
            id=new_id(),
            name=name,
            role_ids=role_ids,
            edges=edges,
            default_model_profile_id=default_model_profile_id,
            role_model_profile_ids=role_model_profile_ids or {},
            is_default=is_default,
            enabled=enabled,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_agent_graph(self, graph_id: str | None) -> AgentGraphRecord | None:
        if graph_id is None:
            return None
        return await self.session.get(AgentGraphRecord, graph_id)

    async def get_default_agent_graph(self) -> AgentGraphRecord | None:
        result = await self.session.execute(
            select(AgentGraphRecord)
            .where(AgentGraphRecord.enabled.is_(True), AgentGraphRecord.is_default.is_(True))
            .order_by(AgentGraphRecord.name)
        )
        return result.scalars().first()

    async def list_agent_graphs(
        self,
        enabled_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[AgentGraphRecord]:
        query = select(AgentGraphRecord).order_by(AgentGraphRecord.is_default.desc(), AgentGraphRecord.name)
        if enabled_only:
            query = query.where(AgentGraphRecord.enabled.is_(True))
        result = await self.session.execute(query.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def update_agent_graph(self, graph_id: str, values: dict[str, Any]) -> AgentGraphRecord:
        record = await self.get_agent_graph(graph_id)
        if record is None:
            raise LookupError(f"agent graph not found: {graph_id}")
        role_ids = values.get("role_ids", record.role_ids)
        edges = [edge.model_dump(mode="json") if isinstance(edge, AgentGraphEdge) else edge for edge in values.get("edges", record.edges)]
        default_model_profile_id = values.get("default_model_profile_id", record.default_model_profile_id)
        role_model_profile_ids = values.get("role_model_profile_ids", record.role_model_profile_ids)
        await self._validate_graph_refs(role_ids, edges, default_model_profile_id, role_model_profile_ids or {})
        if values.get("is_default") is True:
            await self._clear_default_graphs()
        for key, value in values.items():
            if value is not None or key in {"default_model_profile_id"}:
                if key == "edges":
                    value = edges
                setattr(record, key, value)
        record.updated_at = datetime.now(UTC)
        await self.session.flush()
        return record

    async def ensure_default_configuration(
        self,
        builtin_roles: list[dict[str, Any]],
        ollama_base_url: str,
        ollama_model: str,
    ) -> None:
        profile = await self._ensure_model_profile(
            name="ollama default",
            provider_type=ModelProviderType.OLLAMA,
            base_url=ollama_base_url,
            model=ollama_model,
        )
        role_ids: list[str] = []
        worker_ids: list[str] = []
        for role in builtin_roles:
            record = await self.get_agent_role_by_name(str(role["name"]))
            if record is None:
                record = await self.create_agent_role(
                    name=str(role["name"]),
                    mission=str(role["mission"]),
                    non_goals=list(role.get("non_goals", [])),
                    allowed_tools=list(role.get("allowed_tools", [])),
                    requires_approval_for=list(role.get("requires_approval_for", [])),
                    output_contract=str(role.get("output_contract", "")),
                    builtin=True,
                )
            role_ids.append(record.id)
            if record.name not in {"supervisor", "reviewer"}:
                worker_ids.append(record.id)
        if await self.get_default_agent_graph() is None:
            by_name = {role.name: role for role in await self.list_agent_roles(enabled_only=True, limit=1000)}
            edges: list[dict[str, str]] = []
            supervisor = by_name.get("supervisor")
            reviewer = by_name.get("reviewer")
            if supervisor is not None and reviewer is not None:
                for worker_id in worker_ids:
                    edges.append({"from_role": supervisor.id, "to_role": worker_id})
                    edges.append({"from_role": worker_id, "to_role": reviewer.id})
            await self.create_agent_graph(
                name="default",
                role_ids=role_ids,
                edges=edges,
                default_model_profile_id=profile.id,
                is_default=True,
            )

    async def _ensure_model_profile(
        self,
        name: str,
        provider_type: ModelProviderType,
        base_url: str | None,
        model: str,
    ) -> ModelProfileRecord:
        result = await self.session.execute(select(ModelProfileRecord).where(ModelProfileRecord.name == name))
        record = result.scalars().first()
        if record is not None:
            return record
        return await self.create_model_profile(
            name=name,
            provider_type=provider_type,
            base_url=base_url,
            model=model,
            enabled=True,
        )

    async def _clear_default_graphs(self) -> None:
        for graph in await self.list_agent_graphs(limit=1000):
            graph.is_default = False

    async def _validate_graph_refs(
        self,
        role_ids: list[str],
        edges: list[dict[str, str]],
        default_model_profile_id: str | None,
        role_model_profile_ids: dict[str, str],
    ) -> None:
        if default_model_profile_id and await self.get_model_profile(default_model_profile_id) is None:
            raise LookupError(f"model profile not found: {default_model_profile_id}")
        for profile_id in role_model_profile_ids.values():
            if await self.get_model_profile(profile_id) is None:
                raise LookupError(f"model profile not found: {profile_id}")
        known_roles = set(role_ids)
        for role_id in role_ids:
            if await self.get_agent_role(role_id) is None:
                raise LookupError(f"agent role not found: {role_id}")
        for edge in edges:
            source = edge.get("from_role")
            target = edge.get("to_role")
            if source not in known_roles or target not in known_roles:
                raise ValueError("agent graph edges must reference role_ids")
        if _has_cycle(role_ids, edges):
            raise ValueError("agent graph must be acyclic")


def to_run_response(run: RunRecord) -> RunResponse:
    return RunResponse(
        id=run.id,
        thread_id=run.thread_id,
        status=RunStatus(run.status),
        mode=RunMode(run.mode),
        task=run.task,
        workspace=run.workspace,
        model_provider=run.model_provider,
        default_model_profile_id=run.default_model_profile_id,
        role_model_profile_ids={str(key): str(value) for key, value in (run.role_model_profile_ids or {}).items()},
        agent_graph_id=run.agent_graph_id,
        agent_graph_snapshot=run.agent_graph_snapshot or {},
        observability_trace_id=run.observability_trace_id,
        final_answer=run.final_answer,
        error=run.error,
        worker_id=run.worker_id,
        queued_at=run.queued_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        heartbeat_at=run.heartbeat_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def to_secret_response(secret: SecretRecord) -> SecretResponse:
    return SecretResponse(
        id=secret.id,
        name=secret.name,
        secret_set=bool(secret.encrypted_value),
        created_at=secret.created_at,
        updated_at=secret.updated_at,
    )


def to_model_profile_response(profile: ModelProfileRecord) -> ModelProfileResponse:
    return ModelProfileResponse(
        id=profile.id,
        name=profile.name,
        provider_type=ModelProviderType(profile.provider_type),
        base_url=profile.base_url,
        model=profile.model,
        options=profile.options or {},
        secret_id=profile.secret_id,
        secret_set=profile.secret_id is not None,
        enabled=profile.enabled,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def to_agent_role_response(role: AgentRoleRecord) -> AgentRoleResponse:
    return AgentRoleResponse(
        id=role.id,
        name=role.name,
        mission=role.mission,
        non_goals=role.non_goals or [],
        allowed_tools=role.allowed_tools or [],
        requires_approval_for=role.requires_approval_for or [],
        output_contract=role.output_contract,
        builtin=role.builtin,
        enabled=role.enabled,
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


def to_agent_graph_response(graph: AgentGraphRecord) -> AgentGraphResponse:
    return AgentGraphResponse(
        id=graph.id,
        name=graph.name,
        role_ids=graph.role_ids or [],
        edges=[AgentGraphEdge.model_validate(edge) for edge in (graph.edges or [])],
        default_model_profile_id=graph.default_model_profile_id,
        role_model_profile_ids={str(key): str(value) for key, value in (graph.role_model_profile_ids or {}).items()},
        is_default=graph.is_default,
        enabled=graph.enabled,
        created_at=graph.created_at,
        updated_at=graph.updated_at,
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


def _has_cycle(role_ids: list[str], edges: list[dict[str, str]]) -> bool:
    children: dict[str, list[str]] = {role_id: [] for role_id in role_ids}
    for edge in edges:
        source = edge.get("from_role")
        target = edge.get("to_role")
        if source in children and target in children:
            children[source].append(target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(role_id: str) -> bool:
        if role_id in visiting:
            return True
        if role_id in visited:
            return False
        visiting.add(role_id)
        for child in children.get(role_id, []):
            if visit(child):
                return True
        visiting.remove(role_id)
        visited.add(role_id)
        return False

    return any(visit(role_id) for role_id in role_ids)


def _truncate_json_payload(payload: dict[str, Any], max_bytes: int) -> dict[str, Any]:
    try:
        encoded = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    except TypeError:
        payload = json.loads(json.dumps(payload, ensure_ascii=False, default=str))
        encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= max_bytes:
        return payload
    preview = encoded[: max(0, max_bytes - 256)].decode("utf-8", errors="replace")
    return {
        "_truncated": True,
        "original_size_bytes": len(encoded),
        "max_size_bytes": max_bytes,
        "preview": preview,
    }


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)
