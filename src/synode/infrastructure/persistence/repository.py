from __future__ import annotations

import json
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from synode.domain.models import (
    AgentGraphNode,
    AgentGraphNodeEdge,
    AgentGraphNodeKind,
    ApprovalStatus,
    EventType,
    InteractionMode,
    MCPServerTransport,
    ModelProviderType,
    NodeExecutionStatus,
    OperatorRequestKind,
    OperatorRequestStatus,
    OperatorResponseType,
    RoleName,
    RunMode,
    RunStatus,
    RuntimeBackend,
    ThreadMessageAuthorType,
    ThreadMessageType,
    ThreadStatus,
    ToolRisk,
)
from synode.domain.runtime.capabilities import validate_backend_contract
from synode.domain.runtime.contracts import default_contract_for_role, default_contract_registry
from synode.domain.runtime.loop_policy import normalize_native_loop_mode
from synode.infrastructure.persistence.models import (
    AgentGraphRecord,
    AgentRoleRecord,
    ApprovalRecord,
    ArtifactRecord,
    MCPProxySessionRecord,
    MCPServerRecord,
    ModelProfileRecord,
    OperatorRequestRecord,
    RunEventRecord,
    RunRecord,
    RuntimeNodeStateRecord,
    SecretRecord,
    ThreadMessageRecord,
    ThreadRecord,
    ToolAuditRecord,
    WorkerHeartbeatRecord,
    new_id,
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
        interaction_mode: InteractionMode = InteractionMode.AUTO,
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
            interaction_mode=interaction_mode.value,
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
            RunStatus.WAITING_OPERATOR,
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
        if run.status == RunStatus.WAITING_OPERATOR.value:
            pending_operator = await self.count_operator_requests(run_id, status=OperatorRequestStatus.PENDING)
            if pending_operator:
                raise ValueError(f"run has pending operator requests: {run_id}")
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
            message_type=ThreadMessageType.RUN_REPORT,
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

    async def get_approval(self, approval_id: str) -> ApprovalRecord | None:
        return await self.session.get(ApprovalRecord, approval_id)

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

    async def create_operator_request(
        self,
        *,
        run_id: str,
        thread_id: str,
        kind: OperatorRequestKind | str,
        prompt: str,
        context: dict[str, Any] | None = None,
        proposed_payload: dict[str, Any] | None = None,
        node_id: str | None = None,
        role: str | None = None,
        request_id: str | None = None,
    ) -> OperatorRequestRecord:
        existing = await self.get_operator_request(request_id) if request_id else None
        if existing is not None:
            return existing
        kind_value = kind.value if isinstance(kind, OperatorRequestKind) else str(kind)
        record = OperatorRequestRecord(
            id=request_id or new_id(),
            run_id=run_id,
            thread_id=thread_id,
            node_id=node_id,
            role=role,
            kind=kind_value,
            prompt=prompt,
            context=_truncate_json_payload(context or {}, MAX_EVENT_PAYLOAD_BYTES),
            proposed_payload=_truncate_json_payload(proposed_payload or {}, MAX_ARTIFACT_PAYLOAD_BYTES),
            response_payload={},
            status=OperatorRequestStatus.PENDING.value,
        )
        self.session.add(record)
        await self.set_run_status(run_id, RunStatus.WAITING_OPERATOR)
        await self.add_event(
            run_id,
            EventType.OPERATOR_REQUIRED.value,
            role,
            {
                "operator_request_id": record.id,
                "kind": kind_value,
                "node_id": node_id,
                "role": role,
            },
        )
        await self.add_thread_message(
            thread_id,
            author_type=ThreadMessageAuthorType.SYSTEM,
            author_name="operator",
            message_type=ThreadMessageType.OPERATOR_REQUEST,
            content=prompt,
            run_id=run_id,
            metadata={
                "operator_request_id": record.id,
                "kind": kind_value,
                "node_id": node_id,
                "role": role,
            },
        )
        await self.session.flush()
        return record

    async def get_operator_request(self, request_id: str | None) -> OperatorRequestRecord | None:
        if request_id is None:
            return None
        return await self.session.get(OperatorRequestRecord, request_id)

    async def list_operator_requests(
        self,
        run_id: str | None = None,
        status: OperatorRequestStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[OperatorRequestRecord]:
        query = select(OperatorRequestRecord).order_by(OperatorRequestRecord.created_at.desc())
        if run_id is not None:
            query = query.where(OperatorRequestRecord.run_id == run_id)
        if status is not None:
            query = query.where(OperatorRequestRecord.status == status.value)
        result = await self.session.execute(query.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def count_operator_requests(
        self,
        run_id: str,
        status: OperatorRequestStatus | None = None,
    ) -> int:
        query = select(func.count()).select_from(OperatorRequestRecord).where(
            OperatorRequestRecord.run_id == run_id
        )
        if status is not None:
            query = query.where(OperatorRequestRecord.status == status.value)
        return int(await self.session.scalar(query) or 0)

    async def resolve_operator_request(
        self,
        request_id: str,
        response_payload: dict[str, Any],
    ) -> OperatorRequestRecord:
        record = await self.get_operator_request(request_id)
        if record is None:
            raise LookupError(f"operator request not found: {request_id}")
        if record.status != OperatorRequestStatus.PENDING.value:
            raise ValueError(f"operator request already decided: {request_id}")
        response_type = str(response_payload.get("response_type") or "")
        if response_type and response_type not in {item.value for item in OperatorResponseType}:
            raise ValueError(f"unknown operator response_type: {response_type}")
        record.status = OperatorRequestStatus.RESOLVED.value
        record.response_payload = _truncate_json_payload(response_payload, MAX_EVENT_PAYLOAD_BYTES)
        record.resolved_at = datetime.now(UTC)
        await self.add_event(
            record.run_id,
            EventType.OPERATOR_DECIDED.value,
            record.role,
            {
                "operator_request_id": record.id,
                "kind": record.kind,
                "response_type": response_type or None,
            },
        )
        await self.add_thread_message(
            record.thread_id,
            author_type=ThreadMessageAuthorType.SYSTEM,
            author_name="operator",
            message_type=ThreadMessageType.OPERATOR_DECISION,
            content=f"Operator response recorded: {response_type or 'response'}",
            run_id=record.run_id,
            metadata={
                "operator_request_id": record.id,
                "kind": record.kind,
                "response_type": response_type or None,
            },
        )
        await self.session.flush()
        return record

    async def cancel_operator_request(self, request_id: str, reason: str | None = None) -> OperatorRequestRecord:
        record = await self.get_operator_request(request_id)
        if record is None:
            raise LookupError(f"operator request not found: {request_id}")
        if record.status != OperatorRequestStatus.PENDING.value:
            raise ValueError(f"operator request already decided: {request_id}")
        record.status = OperatorRequestStatus.CANCELLED.value
        record.cancelled_at = datetime.now(UTC)
        record.response_payload = {"reason": reason or "cancelled"}
        await self.add_event(
            record.run_id,
            EventType.OPERATOR_CANCELLED.value,
            record.role,
            {
                "operator_request_id": record.id,
                "kind": record.kind,
                "reason": reason,
            },
        )
        await self.session.flush()
        return record

    async def cancel_pending_operator_requests(
        self,
        run_id: str,
        reason: str | None = None,
    ) -> list[OperatorRequestRecord]:
        result = await self.session.execute(
            select(OperatorRequestRecord)
            .where(
                OperatorRequestRecord.run_id == run_id,
                OperatorRequestRecord.status == OperatorRequestStatus.PENDING.value,
            )
            .order_by(OperatorRequestRecord.created_at)
        )
        records = list(result.scalars().all())
        for record in records:
            record.status = OperatorRequestStatus.CANCELLED.value
            record.cancelled_at = datetime.now(UTC)
            record.response_payload = {"reason": reason or "cancelled"}
            await self.add_event(
                record.run_id,
                EventType.OPERATOR_CANCELLED.value,
                record.role,
                {
                    "operator_request_id": record.id,
                    "kind": record.kind,
                    "reason": reason,
                },
            )
        await self.session.flush()
        return records

    async def latest_unconsumed_operator_response(self, run_id: str) -> OperatorRequestRecord | None:
        result = await self.session.execute(
            select(OperatorRequestRecord)
            .where(
                OperatorRequestRecord.run_id == run_id,
                OperatorRequestRecord.status == OperatorRequestStatus.RESOLVED.value,
                OperatorRequestRecord.consumed_at.is_(None),
            )
            .order_by(OperatorRequestRecord.resolved_at.asc(), OperatorRequestRecord.created_at.asc())
            .limit(1)
        )
        return result.scalars().first()

    async def mark_operator_request_consumed(self, request_id: str) -> None:
        record = await self.get_operator_request(request_id)
        if record is None:
            raise LookupError(f"operator request not found: {request_id}")
        record.consumed_at = datetime.now(UTC)
        await self.session.flush()

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
            EventType.TOOL_COMPLETED.value,
            role,
            {
                "tool_name": tool_name,
                "risk": risk.value,
                "status": status,
                "approval_id": approval_id,
                "display": _tool_event_display(tool_name, status, output_payload, approval_id=approval_id),
            },
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

    async def upsert_runtime_node_state(
        self,
        run_id: str,
        node_id: str,
        role: str,
        backend_id: str,
        contract_id: str,
        status: NodeExecutionStatus | str,
        *,
        attempt: int = 1,
        external_id: str | None = None,
        approval_id: str | None = None,
        external_state: dict[str, Any] | None = None,
        last_error: str | None = None,
    ) -> RuntimeNodeStateRecord:
        record = await self.get_runtime_node_state(run_id, node_id, attempt=attempt)
        now = datetime.now(UTC)
        status_value = status.value if isinstance(status, NodeExecutionStatus) else str(status)
        if record is None:
            record = RuntimeNodeStateRecord(
                id=new_id(),
                run_id=run_id,
                node_id=node_id,
                role=role,
                backend_id=backend_id,
                contract_id=contract_id,
                status=status_value,
                attempt=attempt,
                external_id=external_id,
                approval_id=approval_id,
                external_state=external_state or {},
                last_error=last_error,
            )
            self.session.add(record)
        else:
            record.role = role
            record.backend_id = backend_id
            record.contract_id = contract_id
            record.status = status_value
            record.updated_at = now
            if external_id is not None:
                record.external_id = external_id
            if approval_id is not None:
                record.approval_id = approval_id
            if external_state is not None:
                record.external_state = external_state
            if last_error is not None:
                record.last_error = last_error
        if status_value == NodeExecutionStatus.COMPLETED.value:
            record.completed_at = now
        await self.session.flush()
        return record

    async def get_runtime_node_state(
        self,
        run_id: str,
        node_id: str,
        *,
        attempt: int = 1,
    ) -> RuntimeNodeStateRecord | None:
        result = await self.session.execute(
            select(RuntimeNodeStateRecord).where(
                RuntimeNodeStateRecord.run_id == run_id,
                RuntimeNodeStateRecord.node_id == node_id,
                RuntimeNodeStateRecord.attempt == attempt,
            )
        )
        return result.scalars().first()

    async def list_runtime_node_states(self, run_id: str) -> list[RuntimeNodeStateRecord]:
        result = await self.session.execute(
            select(RuntimeNodeStateRecord)
            .where(RuntimeNodeStateRecord.run_id == run_id)
            .order_by(RuntimeNodeStateRecord.created_at, RuntimeNodeStateRecord.node_id)
        )
        return list(result.scalars().all())

    async def mark_runtime_node_approval_forwarded(self, run_id: str, node_id: str, attempt: int = 1) -> None:
        record = await self.get_runtime_node_state(run_id, node_id, attempt=attempt)
        if record is None:
            return
        record.approval_forwarded_at = datetime.now(UTC)
        record.updated_at = datetime.now(UTC)
        await self.session.flush()

    async def mark_runtime_node_cancel_requested(self, run_id: str, node_id: str, attempt: int = 1) -> None:
        record = await self.get_runtime_node_state(run_id, node_id, attempt=attempt)
        if record is None:
            return
        record.cancel_requested_at = datetime.now(UTC)
        record.updated_at = datetime.now(UTC)
        await self.session.flush()

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

    async def create_mcp_server(
        self,
        name: str,
        transport: MCPServerTransport,
        config: dict[str, Any],
        enabled: bool = True,
    ) -> MCPServerRecord:
        record = MCPServerRecord(
            id=new_id(),
            name=name,
            transport=transport.value,
            config=config,
            enabled=enabled,
            tools=[],
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_mcp_server(self, server_id: str | None) -> MCPServerRecord | None:
        if server_id is None:
            return None
        return await self.session.get(MCPServerRecord, server_id)

    async def list_mcp_servers(
        self,
        enabled_only: bool = False,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MCPServerRecord]:
        query = select(MCPServerRecord).order_by(MCPServerRecord.name)
        if enabled_only:
            query = query.where(MCPServerRecord.enabled.is_(True))
        result = await self.session.execute(query.limit(limit).offset(offset))
        return list(result.scalars().all())

    async def update_mcp_server(self, server_id: str, values: dict[str, Any]) -> MCPServerRecord:
        record = await self.get_mcp_server(server_id)
        if record is None:
            raise LookupError(f"MCP server not found: {server_id}")
        config_changed = "config" in values or "transport" in values
        for key, value in values.items():
            if value is None:
                continue
            if key == "transport" and isinstance(value, MCPServerTransport):
                value = value.value
            setattr(record, key, value)
        if config_changed:
            record.tools = []
            record.last_error = None
            record.last_discovered_at = None
        record.updated_at = datetime.now(UTC)
        await self.session.flush()
        return record

    async def delete_mcp_server(self, server_id: str) -> None:
        record = await self.get_mcp_server(server_id)
        if record is None:
            raise LookupError(f"MCP server not found: {server_id}")
        await self.session.delete(record)
        await self.session.flush()

    async def record_mcp_discovery(
        self,
        server_id: str,
        *,
        tools: list[str] | None = None,
        error: str | None = None,
    ) -> MCPServerRecord:
        record = await self.get_mcp_server(server_id)
        if record is None:
            raise LookupError(f"MCP server not found: {server_id}")
        if tools is not None:
            record.tools = sorted(set(tools))
            record.last_error = None
            record.last_discovered_at = datetime.now(UTC)
        if error is not None:
            record.last_error = error
            record.last_discovered_at = datetime.now(UTC)
        record.updated_at = datetime.now(UTC)
        await self.session.flush()
        return record

    async def create_mcp_proxy_session(
        self,
        *,
        run_id: str,
        thread_id: str,
        node_id: str,
        role: str,
        backend_id: str,
        workspace: str | None,
        allowed_tools: list[str],
        token_hash: str,
        expires_at: datetime,
    ) -> MCPProxySessionRecord:
        record = MCPProxySessionRecord(
            id=new_id(),
            run_id=run_id,
            thread_id=thread_id,
            node_id=node_id,
            role=role,
            backend_id=backend_id,
            workspace=workspace,
            allowed_tools=sorted(set(allowed_tools)),
            token_hash=token_hash,
            expires_at=expires_at,
        )
        self.session.add(record)
        await self.session.flush()
        return record

    async def get_mcp_proxy_session(self, session_id: str | None) -> MCPProxySessionRecord | None:
        if session_id is None:
            return None
        return await self.session.get(MCPProxySessionRecord, session_id)

    async def touch_mcp_proxy_session(self, session_id: str) -> None:
        record = await self.get_mcp_proxy_session(session_id)
        if record is None:
            raise LookupError(f"MCP proxy session not found: {session_id}")
        record.last_used_at = datetime.now(UTC)
        await self.session.flush()

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
        nodes: list[dict[str, Any]],
        node_edges: list[dict[str, str]],
        graph_schema_version: int = 2,
        default_model_profile_id: str | None = None,
        role_model_profile_ids: dict[str, str] | None = None,
        node_runtime_bindings: Mapping[str, str | RuntimeBackend] | None = None,
        node_contracts: Mapping[str, str] | None = None,
        node_loop_policies: Mapping[str, str] | None = None,
        is_default: bool = False,
        enabled: bool = True,
    ) -> AgentGraphRecord:
        graph_config = await self._normalize_graph_config(
            default_model_profile_id,
            role_model_profile_ids or {},
            graph_schema_version=graph_schema_version,
            nodes=nodes,
            node_edges=node_edges,
            node_runtime_bindings=node_runtime_bindings or {},
            node_contracts=node_contracts or {},
            node_loop_policies=node_loop_policies or {},
        )
        if is_default:
            await self._clear_default_graphs()
        record = AgentGraphRecord(
            id=new_id(),
            name=name,
            graph_schema_version=graph_config["graph_schema_version"],
            nodes=graph_config["nodes"],
            node_edges=graph_config["node_edges"],
            default_model_profile_id=default_model_profile_id,
            role_model_profile_ids=role_model_profile_ids or {},
            node_runtime_bindings=graph_config["node_runtime_bindings"],
            node_contracts=graph_config["node_contracts"],
            node_loop_policies=graph_config["node_loop_policies"],
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
        nodes = _node_dicts(values.get("nodes", record.nodes))
        node_edges = _node_edge_dicts(values.get("node_edges", record.node_edges))
        default_model_profile_id = values.get("default_model_profile_id", record.default_model_profile_id)
        role_model_profile_ids = values.get("role_model_profile_ids", record.role_model_profile_ids)
        node_runtime_bindings = values.get("node_runtime_bindings", record.node_runtime_bindings) or {}
        node_contracts = values.get("node_contracts", record.node_contracts) or {}
        node_loop_policies = values.get("node_loop_policies", record.node_loop_policies) or {}
        graph_config = await self._normalize_graph_config(
            default_model_profile_id,
            role_model_profile_ids or {},
            graph_schema_version=values.get("graph_schema_version", record.graph_schema_version),
            nodes=nodes,
            node_edges=node_edges,
            node_runtime_bindings=node_runtime_bindings,
            node_contracts=node_contracts,
            node_loop_policies=node_loop_policies,
        )
        if values.get("is_default") is True:
            await self._clear_default_graphs()
        graph_keys = {
            "graph_schema_version",
            "nodes",
            "node_edges",
            "node_runtime_bindings",
            "node_contracts",
            "node_loop_policies",
        }
        if graph_keys.intersection(values):
            for key in graph_keys:
                setattr(record, key, graph_config[key])
        for key, value in values.items():
            if key in graph_keys:
                continue
            if value is not None or key in {"default_model_profile_id"}:
                if key == "role_model_profile_ids":
                    value = role_model_profile_ids or {}
                elif key == "default_model_profile_id":
                    value = default_model_profile_id
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
        for role in builtin_roles:
            mission = str(role["mission"])
            non_goals = list(role.get("non_goals", []))
            allowed_tools = list(role.get("allowed_tools", []))
            requires_approval_for = list(role.get("requires_approval_for", []))
            output_contract = str(role.get("output_contract", ""))
            values = {
                "mission": mission,
                "non_goals": non_goals,
                "allowed_tools": allowed_tools,
                "requires_approval_for": requires_approval_for,
                "output_contract": output_contract,
                "builtin": True,
                "enabled": True,
            }
            record = await self.get_agent_role_by_name(str(role["name"]))
            if record is None:
                record = await self.create_agent_role(
                    name=str(role["name"]),
                    mission=mission,
                    non_goals=non_goals,
                    allowed_tools=allowed_tools,
                    requires_approval_for=requires_approval_for,
                    output_contract=output_contract,
                    builtin=True,
                    enabled=True,
                )
            else:
                await self.update_agent_role(
                    record.id,
                    values,
                )
        if await self.get_default_agent_graph() is None:
            by_name = {role.name: role for role in await self.list_agent_roles(enabled_only=True, limit=1000)}
            supervisor = by_name.get("supervisor")
            reviewer = by_name.get("reviewer")
            workers = [
                role
                for role in by_name.values()
                if role.name not in {"supervisor", "reviewer"}
            ]
            nodes = _graph_nodes_from_roles([role for role in [supervisor, *workers, reviewer] if role is not None])
            node_edges: list[dict[str, str]] = []
            if supervisor is not None and reviewer is not None:
                for worker in workers:
                    node_edges.append({"from_node": _slug(supervisor.name), "to_node": _slug(worker.name)})
                    node_edges.append({"from_node": _slug(worker.name), "to_node": _slug(reviewer.name)})
            await self.create_agent_graph(
                name="default",
                nodes=nodes,
                node_edges=node_edges,
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

    async def _normalize_graph_config(
        self,
        default_model_profile_id: str | None,
        role_model_profile_ids: dict[str, str],
        *,
        graph_schema_version: int,
        nodes: list[dict[str, Any]],
        node_edges: list[dict[str, str]],
        node_runtime_bindings: Mapping[str, str | RuntimeBackend],
        node_contracts: Mapping[str, str],
        node_loop_policies: Mapping[str, str],
    ) -> dict[str, Any]:
        if default_model_profile_id and await self.get_model_profile(default_model_profile_id) is None:
            raise LookupError(f"model profile not found: {default_model_profile_id}")
        for profile_id in role_model_profile_ids.values():
            if await self.get_model_profile(profile_id) is None:
                raise LookupError(f"model profile not found: {profile_id}")
        if graph_schema_version != 2:
            raise ValueError("agent graph schema version must be 2")

        roles = {role.id: role for role in await self.list_agent_roles(enabled_only=False, limit=1000)}
        raw_nodes = _node_dicts(nodes)
        raw_node_edges = _node_edge_dicts(node_edges)
        if not raw_nodes:
            raise ValueError("agent graph nodes are required")
        normalized_nodes = await self._normalize_explicit_nodes(raw_nodes, roles)
        normalized_node_edges = raw_node_edges

        node_by_id = {node["id"]: node for node in normalized_nodes}
        role_id_to_node = {node["role_id"]: node["id"] for node in normalized_nodes}
        if len(role_id_to_node) != len(normalized_nodes):
            raise ValueError("agent graph cannot contain duplicate role nodes")
        for edge in normalized_node_edges:
            if edge["from_node"] not in node_by_id or edge["to_node"] not in node_by_id:
                raise ValueError("agent graph node_edges must reference nodes")
        if _has_node_cycle(list(node_by_id), normalized_node_edges):
            raise ValueError("agent graph must be acyclic")

        contracts = self._resolve_node_contracts(normalized_nodes, roles, node_contracts)
        runtime_by_node = self._resolve_node_runtime_bindings(
            normalized_nodes,
            node_runtime_bindings,
        )
        loop_policies = self._resolve_node_loop_policies(normalized_nodes, node_loop_policies)
        for node_id, backend in runtime_by_node.items():
            validate_backend_contract(backend, contracts[node_id])
        return {
            "graph_schema_version": 2,
            "nodes": normalized_nodes,
            "node_edges": normalized_node_edges,
            "node_runtime_bindings": runtime_by_node,
            "node_contracts": contracts,
            "node_loop_policies": loop_policies,
        }

    async def _normalize_explicit_nodes(
        self,
        nodes: list[dict[str, Any]],
        roles: dict[str, AgentRoleRecord],
    ) -> list[dict[str, str]]:
        normalized: list[dict[str, str]] = []
        seen_ids: set[str] = set()
        seen_roles: set[str] = set()
        for raw_node in nodes:
            node = AgentGraphNode.model_validate(raw_node)
            if node.id in seen_ids:
                raise ValueError(f"duplicate agent graph node id: {node.id}")
            if node.role_id in seen_roles:
                raise ValueError(f"duplicate agent graph role node: {node.role_id}")
            role = roles.get(node.role_id)
            if role is None:
                raise LookupError(f"agent role not found: {node.role_id}")
            expected_kind = _expected_node_kind(role.name)
            if node.kind != expected_kind:
                raise ValueError(f"agent graph node {node.id} must be {expected_kind.value}")
            normalized.append(node.model_dump(mode="json"))
            seen_ids.add(node.id)
            seen_roles.add(node.role_id)
        return normalized

    def _resolve_node_runtime_bindings(
        self,
        nodes: list[dict[str, str]],
        node_runtime_bindings: Mapping[str, str | RuntimeBackend],
    ) -> dict[str, str]:
        resolved = {node["id"]: RuntimeBackend.NATIVE_LANGGRAPH.value for node in nodes}
        for node_id, backend in node_runtime_bindings.items():
            if str(node_id) not in resolved:
                raise LookupError(f"agent graph node not found: {node_id}")
            resolved[str(node_id)] = _runtime_backend_as_value(backend)
        return resolved

    def _resolve_node_contracts(
        self,
        nodes: list[dict[str, str]],
        roles: dict[str, AgentRoleRecord],
        node_contracts: Mapping[str, str],
    ) -> dict[str, str]:
        registry = default_contract_registry()
        resolved: dict[str, str] = {}
        for node in nodes:
            role = roles[node["role_id"]]
            contract_id = str(node_contracts.get(node["id"]) or default_contract_for_role(role.name))
            registry.validate_binding(
                contract_id,
                role_name=role.name,
                node_kind=AgentGraphNodeKind(node["kind"]),
            )
            resolved[node["id"]] = contract_id
        unknown = set(node_contracts) - {node["id"] for node in nodes}
        if unknown:
            raise LookupError(f"agent graph node not found: {sorted(unknown)}")
        return resolved

    def _resolve_node_loop_policies(
        self,
        nodes: list[dict[str, str]],
        node_loop_policies: Mapping[str, str],
    ) -> dict[str, str]:
        node_ids = {node["id"] for node in nodes}
        unknown = set(node_loop_policies) - node_ids
        if unknown:
            raise LookupError(f"agent graph node not found: {sorted(unknown)}")
        return {
            str(node_id): normalize_native_loop_mode(mode)
            for node_id, mode in node_loop_policies.items()
        }


def _thread_title(value: str) -> str:
    title = " ".join(value.strip().split())
    if not title:
        return "Untitled thread"
    return title[:120]


def _has_node_cycle(node_ids: list[str], edges: list[dict[str, str]]) -> bool:
    children: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for edge in edges:
        source = edge.get("from_node")
        target = edge.get("to_node")
        if source in children and target in children:
            children[source].append(target)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node_id: str) -> bool:
        if node_id in visiting:
            return True
        if node_id in visited:
            return False
        visiting.add(node_id)
        for child in children.get(node_id, []):
            if visit(child):
                return True
        visiting.remove(node_id)
        visited.add(node_id)
        return False

    return any(visit(node_id) for node_id in node_ids)


def _node_dicts(nodes: list[Any] | None) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for node in nodes or []:
        payload = node.model_dump(mode="json") if isinstance(node, AgentGraphNode) else node
        if not isinstance(payload, dict):
            raise ValueError("agent graph nodes must be objects")
        normalized.append(AgentGraphNode.model_validate(payload).model_dump(mode="json"))
    return normalized


def _node_edge_dicts(edges: list[Any] | None) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for edge in edges or []:
        payload = edge.model_dump(mode="json") if isinstance(edge, AgentGraphNodeEdge) else edge
        if not isinstance(payload, dict):
            raise ValueError("agent graph node_edges must be objects")
        normalized.append(AgentGraphNodeEdge.model_validate(payload).model_dump(mode="json"))
    return normalized


def _expected_node_kind(role_name: str) -> AgentGraphNodeKind:
    if role_name in {RoleName.SUPERVISOR.value, RoleName.REVIEWER.value}:
        return AgentGraphNodeKind.CONTROL
    return AgentGraphNodeKind.WORKER


def _slug(value: str) -> str:
    slug = "".join(ch.lower() if ch.isalnum() else "_" for ch in value)
    compact = "_".join(part for part in slug.split("_") if part)
    return compact or "node"


def _graph_nodes_from_roles(roles: list[AgentRoleRecord]) -> list[dict[str, str]]:
    return [
        {
            "id": _slug(role.name),
            "role_id": role.id,
            "label": role.name,
            "kind": _expected_node_kind(role.name).value,
        }
        for role in roles
    ]


def _runtime_backend_as_value(backend: str | RuntimeBackend) -> str:
    value = str(backend.value if isinstance(backend, RuntimeBackend) else backend)
    known = {RuntimeBackend.NATIVE_LANGGRAPH.value, RuntimeBackend.OPENHANDS.value}
    if value not in known:
        raise ValueError(f"unknown runtime backend: {value}")
    return value


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


def _tool_event_display(
    tool_name: str,
    status: str,
    output_payload: dict[str, Any],
    *,
    approval_id: str | None,
) -> dict[str, Any]:
    if approval_id:
        title = f"{tool_name} needs approval"
        tone = "warning"
    elif status in {"ok", "approved"}:
        title = f"{tool_name} completed"
        tone = "success"
    elif status == "denied":
        title = f"{tool_name} denied"
        tone = "danger"
    else:
        title = f"{tool_name} failed"
        tone = "danger"
    target = _tool_event_target(output_payload)
    return {
        "title": title,
        "status": status,
        "tone": tone,
        "target": target,
    }


def _tool_event_target(output_payload: dict[str, Any]) -> str | None:
    output = output_payload.get("output")
    if not isinstance(output, dict):
        output = output_payload
    for key in ("path", "file", "command", "query", "url"):
        value = output.get(key)
        if value:
            return _compact_text(value, 180)
    error = output_payload.get("error")
    if error:
        return _compact_text(error, 180)
    return None


def _compact_text(value: Any, limit: int) -> str:
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _rowcount(result: Any) -> int:
    return int(getattr(result, "rowcount", 0) or 0)
