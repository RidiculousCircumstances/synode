from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from typing import Any, AsyncIterator

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

from synode.application.reports import build_run_report
from synode.config import Settings
from synode.logging import log_event
from synode.models.provider import ModelProviderRegistry, ModelRequest
from synode.observability import Observability
from synode.persistence.database import Database
from synode.persistence.repository import (
    Repository,
    to_agent_graph_response,
    to_agent_role_response,
    to_mcp_server_response,
    to_model_profile_response,
    to_operator_request_response,
    to_run_response,
    to_secret_response,
    to_thread_message_response,
    to_thread_response,
)
from synode.registry import RoleRegistry, RoleSpec
from synode.runtime.capabilities import validate_backend_contract
from synode.runtime.contracts import default_contract_for_role, default_contract_registry
from synode.runtime.execution import ExecutionBackendRegistry, build_execution_backend_registry
from synode.runtime.graph import (
    GraphDependencies,
    build_graph,
    resume_native_coding_after_patch_approval,
)
from synode.runtime.operator import ApprovalRequired, OperatorInterrupt, OperatorRejected
from synode.runtime.queue import (
    MissingRunQueueTransport,
    RunQueueTransport,
    build_run_queue_transport,
)
from synode.schemas import (
    AgentGraphCreateRequest,
    AgentGraphResponse,
    AgentGraphUpdateRequest,
    AgentRoleCreateRequest,
    AgentRoleResponse,
    AgentRoleUpdateRequest,
    ApprovalResponse,
    ApprovalStatus,
    ArtifactResponse,
    EventType,
    ExecutionBackendStatusResponse,
    GpuMetrics,
    InteractionMode,
    MCPServerCreateRequest,
    MCPServerResponse,
    MCPServerTransport,
    MCPServerUpdateRequest,
    ModelProfileCreateRequest,
    ModelProfileResponse,
    ModelProfileStructuredProbe,
    ModelProfileTestCapabilities,
    ModelProfileTestCheck,
    ModelProfileTestResponse,
    ModelProfileUpdateRequest,
    ModelProviderType,
    OperatorRequestDecision,
    OperatorRequestKind,
    OperatorRequestResponse,
    OperatorRequestStatus,
    OperatorResponseType,
    ProcessMetrics,
    QueueStatusResponse,
    RoleName,
    RunEventResponse,
    RunMetricsResponse,
    RunMode,
    RunReportResponse,
    RunResponse,
    RunStatus,
    RuntimeBackend,
    RuntimeStatusResponse,
    SandboxStatusResponse,
    SecretCreateRequest,
    SecretResponse,
    SecretUpdateRequest,
    SystemMetricsResponse,
    ThreadDetailResponse,
    ThreadMessageAuthorType,
    ThreadMessageResponse,
    ThreadMessageType,
    ThreadResponse,
    ThreadStatus,
    TokenUsage,
    ToolAuditResponse,
    ToolRisk,
    WorkerHeartbeatResponse,
)
from synode.security import SecretCipher
from synode.tools import ToolExecutor, ToolRegistry, build_tool_registry
from synode.tools.mcp import MCPServerRuntimeConfig, discover_mcp_tools, register_mcp_tools
from synode.tools.sandbox import SandboxRunner

logger = logging.getLogger(__name__)

ACTIVE_RUN_STATUSES = {
    RunStatus.CREATED.value,
    RunStatus.QUEUED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_APPROVAL.value,
    RunStatus.WAITING_OPERATOR.value,
    RunStatus.CANCELLING.value,
}

TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.FAILED_VERIFICATION.value,
    RunStatus.CANCELLED.value,
}

CONVERSATION_CONTEXT_MESSAGE_LIMIT = 30
CONVERSATION_CONTEXT_SOURCE_LIMIT = 80
CONVERSATION_CONTEXT_CHAR_LIMIT = 12_000
CONVERSATION_CONTEXT_MESSAGE_TYPES = {
    ThreadMessageType.TEXT.value,
    ThreadMessageType.FINAL.value,
    ThreadMessageType.RUN_REPORT.value,
    ThreadMessageType.APPROVAL_DECISION.value,
    ThreadMessageType.OPERATOR_DECISION.value,
}


class OrchestrationService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        roles: RoleRegistry,
        models: ModelProviderRegistry,
        tools: ToolRegistry,
        observability: Observability | None = None,
        run_queue: RunQueueTransport | None = None,
        execution_backends: ExecutionBackendRegistry | None = None,
    ):
        self.settings = settings
        self.database = database
        self.roles = roles
        self.models = models
        self.tools = tools
        self.observability = observability or Observability(settings)
        self.run_queue: RunQueueTransport = run_queue or MissingRunQueueTransport()
        self.execution_backends = execution_backends or build_execution_backend_registry(settings, database)
        self.secret_cipher = SecretCipher(settings) if settings.secrets_key else None
        self.tool_executor = ToolExecutor(database, roles, tools, settings, self.observability)
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_reasons: dict[str, str] = {}
        self._memory_checkpointer = MemorySaver()

    async def start_run(self, run_id: str) -> None:
        log_fields: dict[str, Any] = {}
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                raise ValueError(f"run is terminal: {run_id}")
            if run.status in {RunStatus.RUNNING.value, RunStatus.CANCELLING.value}:
                raise ValueError(f"run is already active: {run_id}")
            await repo.enqueue_run(run_id)
            log_fields = {
                "run_id": run.id,
                "thread_id": run.thread_id,
                "trace_id": run.observability_trace_id,
                "provider": run.model_provider,
            }
        try:
            await self.run_queue.enqueue_run(run_id)
        except Exception as exc:
            log_event(
                logger,
                "run_queue_enqueue_failed",
                **log_fields,
                queue_backend=self.run_queue.backend,
                error_class=exc.__class__.__name__,
                error=str(exc),
            )
            raise
        log_event(
            logger,
            EventType.RUN_QUEUED.value,
            **log_fields,
            queue_backend=self.run_queue.backend,
        )

    async def _execute_run_background(self, run_id: str) -> None:
        try:
            await self.execute_run(run_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            # execute_run records failure state and thread-visible error messages.
            pass
        finally:
            task = self._run_tasks.get(run_id)
            if task is asyncio.current_task():
                self._run_tasks.pop(run_id, None)

    async def create_run(
        self,
        task: str,
        workspace: str | None = None,
        model_provider: str | None = None,
        mode: RunMode = RunMode.GENERAL,
        default_model_profile_id: str | None = None,
        role_model_profile_ids: dict[str, str] | None = None,
        agent_graph_id: str | None = None,
        interaction_mode: InteractionMode = InteractionMode.AUTO,
    ) -> RunResponse:
        trace_id = self.observability.create_trace_id()
        async with self.database.session() as session:
            repo = Repository(session)
            config = await self._prepare_run_config(
                repo,
                mode=mode,
                model_provider=model_provider,
                default_model_profile_id=default_model_profile_id,
                role_model_profile_ids=role_model_profile_ids or {},
                agent_graph_id=agent_graph_id,
            )
            run = await repo.create_run(
                task=task,
                workspace=workspace,
                model_provider=config["model_provider"],
                mode=mode,
                observability_trace_id=trace_id,
                default_model_profile_id=config["default_model_profile_id"],
                role_model_profile_ids=config["role_model_profile_ids"],
                agent_graph_id=config["agent_graph_id"],
                agent_graph_snapshot=config["agent_graph_snapshot"],
                interaction_mode=interaction_mode,
            )
            return to_run_response(run)

    async def create_thread(
        self,
        message: str,
        title: str | None = None,
        workspace: str | None = None,
        model_provider: str | None = None,
        mode: RunMode = RunMode.GENERAL,
        default_model_profile_id: str | None = None,
        role_model_profile_ids: dict[str, str] | None = None,
        agent_graph_id: str | None = None,
        interaction_mode: InteractionMode = InteractionMode.AUTO,
    ) -> ThreadDetailResponse:
        trace_id = self.observability.create_trace_id()
        async with self.database.session() as session:
            repo = Repository(session)
            config = await self._prepare_run_config(
                repo,
                mode=mode,
                model_provider=model_provider,
                default_model_profile_id=default_model_profile_id,
                role_model_profile_ids=role_model_profile_ids or {},
                agent_graph_id=agent_graph_id,
            )
            thread = await repo.create_thread(title or message)
            await repo.create_run(
                task=message,
                workspace=workspace,
                model_provider=config["model_provider"],
                mode=mode,
                observability_trace_id=trace_id,
                thread_id=thread.id,
                default_model_profile_id=config["default_model_profile_id"],
                role_model_profile_ids=config["role_model_profile_ids"],
                agent_graph_id=config["agent_graph_id"],
                agent_graph_snapshot=config["agent_graph_snapshot"],
                interaction_mode=interaction_mode,
            )
            return await self._thread_detail(repo, thread.id)

    async def list_threads(
        self,
        status: ThreadStatus | None = ThreadStatus.ACTIVE,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ThreadResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            threads = await repo.list_threads(status=status, search=search, limit=limit, offset=offset)
            responses: list[ThreadResponse] = []
            for thread in threads:
                responses.append(
                    to_thread_response(
                        thread,
                        latest_run=await repo.latest_thread_run(thread.id),
                        latest_message=await repo.latest_thread_message(thread.id),
                    )
                )
            return responses

    async def get_thread(
        self,
        thread_id: str,
        runs_limit: int = 50,
        runs_offset: int = 0,
        messages_limit: int = 200,
        messages_offset: int = 0,
    ) -> ThreadDetailResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            return await self._thread_detail(
                repo,
                thread_id,
                runs_limit=runs_limit,
                runs_offset=runs_offset,
                messages_limit=messages_limit,
                messages_offset=messages_offset,
            )

    async def update_thread_title(self, thread_id: str, title: str) -> ThreadResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            thread = await repo.update_thread_title(thread_id, title)
            return to_thread_response(
                thread,
                latest_run=await repo.latest_thread_run(thread.id),
                latest_message=await repo.latest_thread_message(thread.id),
            )

    async def archive_thread(self, thread_id: str) -> ThreadResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            thread = await repo.archive_thread(thread_id)
            return to_thread_response(
                thread,
                latest_run=await repo.latest_thread_run(thread.id),
                latest_message=await repo.latest_thread_message(thread.id),
            )

    async def create_thread_run(
        self,
        thread_id: str,
        message: str,
        workspace: str | None = None,
        model_provider: str | None = None,
        mode: RunMode = RunMode.GENERAL,
        default_model_profile_id: str | None = None,
        role_model_profile_ids: dict[str, str] | None = None,
        agent_graph_id: str | None = None,
        interaction_mode: InteractionMode = InteractionMode.AUTO,
    ) -> RunResponse:
        trace_id = self.observability.create_trace_id()
        async with self.database.session() as session:
            repo = Repository(session)
            thread = await repo.get_thread(thread_id)
            if thread is None:
                raise LookupError(f"thread not found: {thread_id}")
            if thread.status != ThreadStatus.ACTIVE.value:
                raise ValueError(f"thread is not active: {thread_id}")
            latest_run = await repo.latest_thread_run(thread_id)
            if latest_run is not None and latest_run.status in ACTIVE_RUN_STATUSES:
                raise ValueError(f"thread has an active run: {latest_run.id}")
            config = await self._prepare_run_config(
                repo,
                mode=mode,
                model_provider=model_provider,
                default_model_profile_id=default_model_profile_id,
                role_model_profile_ids=role_model_profile_ids or {},
                agent_graph_id=agent_graph_id,
            )
            run = await repo.create_run(
                task=message,
                workspace=workspace,
                model_provider=config["model_provider"],
                mode=mode,
                observability_trace_id=trace_id,
                thread_id=thread_id,
                default_model_profile_id=config["default_model_profile_id"],
                role_model_profile_ids=config["role_model_profile_ids"],
                agent_graph_id=config["agent_graph_id"],
                agent_graph_snapshot=config["agent_graph_snapshot"],
                interaction_mode=interaction_mode,
            )
            return to_run_response(run)

    async def list_thread_messages(
        self,
        thread_id: str,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ThreadMessageResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            if await repo.get_thread(thread_id) is None:
                raise LookupError(f"thread not found: {thread_id}")
            return [
                to_thread_message_response(message)
                for message in await repo.list_thread_messages(thread_id, limit=limit, offset=offset)
            ]

    async def list_runs(
        self,
        status: RunStatus | None = None,
        mode: RunMode | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            runs = await repo.list_runs(status=status, mode=mode, limit=limit, offset=offset)
            return [to_run_response(run) for run in runs]

    async def get_run(self, run_id: str) -> RunResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            return to_run_response(run)

    async def list_events(self, run_id: str, after_id: int = 0, limit: int = 200) -> list[dict[str, Any]]:
        async with self.database.session() as session:
            repo = Repository(session)
            events = await repo.list_events(run_id, after_id=after_id, limit=limit)
            return [
                {
                    "id": event.id,
                    "run_id": event.run_id,
                    "event_type": event.event_type,
                    "role": event.role,
                    "payload": event.payload,
                    "created_at": event.created_at.isoformat(),
                }
                for event in events
            ]

    async def list_event_responses(
        self,
        run_id: str,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[RunEventResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            events = await repo.list_events(run_id, after_id=after_id, limit=limit)
            return [
                RunEventResponse(
                    id=event.id,
                    run_id=event.run_id,
                    event_type=event.event_type,
                    role=event.role,
                    payload=event.payload,
                    created_at=event.created_at,
                )
                for event in events
            ]

    async def list_artifacts(self, run_id: str, limit: int = 100, offset: int = 0) -> list[ArtifactResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            artifacts = await repo.list_artifacts(run_id, limit=limit, offset=offset)
            return [
                ArtifactResponse(
                    id=artifact.id,
                    run_id=artifact.run_id,
                    kind=artifact.kind,
                    path=artifact.path,
                    content=artifact.content,
                    created_at=artifact.created_at,
                )
                for artifact in artifacts
            ]

    async def get_run_report(self, run_id: str) -> RunReportResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            artifact = await repo.get_latest_artifact(run_id, "run_report")
            if artifact is not None:
                return RunReportResponse.model_validate(
                    {
                        **artifact.content,
                        "artifact_id": artifact.id,
                        "created_at": artifact.created_at,
                    }
                )
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            report = build_run_report(
                {
                    "run_id": run.id,
                    "thread_id": run.thread_id,
                    "mode": run.mode,
                    "review": {"blockers": [run.error]} if run.error else {},
                },
                status=run.status,
            )
            return RunReportResponse.model_validate(report.to_artifact())

    async def list_tool_audit(self, run_id: str, limit: int = 200, offset: int = 0) -> list[ToolAuditResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            audit = await repo.list_tool_audit(run_id, limit=limit, offset=offset)
            return [
                ToolAuditResponse(
                    id=record.id,
                    run_id=record.run_id,
                    role=record.role,
                    tool_name=record.tool_name,
                    risk=ToolRisk(record.risk),
                    status=record.status,
                    input=record.input,
                    output=record.output,
                    approval_id=record.approval_id,
                    created_at=record.created_at,
                )
                for record in audit
            ]

    async def list_approvals(
        self,
        run_id: str | None = None,
        status: ApprovalStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ApprovalResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            approvals = await repo.list_approvals(run_id=run_id, status=status, limit=limit, offset=offset)
            return [
                ApprovalResponse(
                    id=approval.id,
                    run_id=approval.run_id,
                    tool_name=approval.tool_name,
                    action=approval.action,
                    reason=approval.reason,
                    payload=approval.payload,
                    status=ApprovalStatus(approval.status),
                    decision_reason=approval.decision_reason,
                    created_at=approval.created_at,
                    decided_at=approval.decided_at,
                )
                for approval in approvals
            ]

    async def list_operator_requests(
        self,
        run_id: str | None = None,
        status: OperatorRequestStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[OperatorRequestResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            records = await repo.list_operator_requests(
                run_id=run_id,
                status=status,
                limit=limit,
                offset=offset,
            )
            return [to_operator_request_response(record) for record in records]

    async def respond_operator_request(
        self,
        request_id: str,
        decision: OperatorRequestDecision,
    ) -> OperatorRequestResponse:
        should_resume = decision.response_type != OperatorResponseType.REJECT
        run_id = ""
        async with self.database.session() as session:
            repo = Repository(session)
            record = await repo.get_operator_request(request_id)
            if record is None:
                raise LookupError(f"operator request not found: {request_id}")
            if record.status != OperatorRequestStatus.PENDING.value:
                raise ValueError(f"operator request already decided: {request_id}")
            run_id = record.run_id
            response_payload = {
                "response_type": decision.response_type.value,
                "message": decision.message,
                "payload": decision.payload,
            }
            record = await repo.resolve_operator_request(request_id, response_payload)
            response = to_operator_request_response(record)
        if should_resume:
            await self.start_run(run_id)
        else:
            await self._mark_run_cancelled(run_id, decision.message or "Operator rejected the run.")
        return response

    async def cancel_operator_request(self, request_id: str, reason: str | None = None) -> OperatorRequestResponse:
        run_id = ""
        async with self.database.session() as session:
            repo = Repository(session)
            record = await repo.cancel_operator_request(request_id, reason)
            response = to_operator_request_response(record)
            run_id = record.run_id
        await self._mark_run_cancelled(run_id, reason or "Operator request cancelled.")
        return response

    async def run_task(
        self,
        task: str,
        workspace: str | None = None,
        model_provider: str | None = None,
        mode: RunMode = RunMode.GENERAL,
        default_model_profile_id: str | None = None,
        role_model_profile_ids: dict[str, str] | None = None,
        agent_graph_id: str | None = None,
        interaction_mode: InteractionMode = InteractionMode.AUTO,
    ) -> RunResponse:
        run = await self.create_run(
            task,
            workspace,
            model_provider,
            mode,
            default_model_profile_id,
            role_model_profile_ids,
            agent_graph_id,
            interaction_mode,
        )
        await self.execute_run(run.id)
        return await self.get_run(run.id)

    async def execute_run(self, run_id: str) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                return
            if run.status == RunStatus.CANCELLING.value:
                await repo.set_run_status(
                    run_id,
                    RunStatus.CANCELLED,
                    final_answer=run.error or "Run stopped by user.",
                    error=run.error or "Run stopped by user.",
                )
                await repo.add_event(
                    run_id,
                    EventType.RUN_CANCELLED.value,
                    None,
                    {"reason": run.error or "Run stopped by user."},
                )
                return
            pending_operator_response = await repo.latest_unconsumed_operator_response(run_id)
            if run.status == RunStatus.WAITING_OPERATOR.value and pending_operator_response is None:
                pending_operator_requests = await repo.count_operator_requests(
                    run_id,
                    status=OperatorRequestStatus.PENDING,
                )
                if pending_operator_requests:
                    return
                raise ValueError(f"run is waiting for operator response: {run_id}")
            resume_operator_request_id = (
                pending_operator_response.id if pending_operator_response is not None else None
            )
            resume_payload = (
                pending_operator_response.response_payload if pending_operator_response is not None else None
            )
            approval_count = await repo.count_approvals(run_id)
            checkpoint_thread_id = (
                run_id
                if resume_payload is not None or approval_count == 0
                else f"{run_id}:approval-restart:{time.time_ns()}"
            )
            await repo.set_run_status(run_id, RunStatus.RUNNING)
            await repo.add_event(run_id, EventType.RUN_STARTED.value, None, {})
            task = run.task
            workspace = run.workspace
            model_provider = run.model_provider
            default_model_profile_id = run.default_model_profile_id
            role_model_profile_ids = {
                str(key): str(value)
                for key, value in (run.role_model_profile_ids or {}).items()
            }
            agent_graph_id = run.agent_graph_id
            agent_graph_snapshot = run.agent_graph_snapshot or {}
            mode = run.mode
            interaction_mode = run.interaction_mode
            trace_id = run.observability_trace_id
            thread_id = run.thread_id
            log_event(
                logger,
                EventType.RUN_STARTED.value,
                run_id=run_id,
                thread_id=thread_id,
                trace_id=trace_id,
                provider=model_provider,
                mode=mode,
            )
            conversation_context = await self._conversation_context(repo, thread_id, run_id)

        try:
            state: dict[str, Any] = {
                "run_id": run_id,
                "thread_id": thread_id,
                "task": task,
                "conversation_context": conversation_context,
                "workspace": workspace,
                "model_provider": model_provider,
                "default_model_profile_id": default_model_profile_id,
                "role_model_profile_ids": role_model_profile_ids,
                "agent_graph_id": agent_graph_id,
                "agent_graph_snapshot": agent_graph_snapshot,
                "mode": mode,
                "interaction_mode": interaction_mode,
                "observability_trace_id": trace_id,
                "worker_outputs": [],
            }
            with self.observability.observation(
                "synode.run",
                trace_id,
                as_type="chain",
                input_payload={"task": task, "workspace": workspace, "mode": mode},
                metadata={"run_id": run_id, "model_provider": model_provider},
            ):
                approved_patch_resume = await self._approved_native_patch_resume_id(run_id, mode)
                if approved_patch_resume is not None and resume_payload is None:
                    final_state = await self._resume_native_coding_after_patch_approval(run_id, state)
                elif resume_payload is None:
                    if checkpoint_thread_id == run_id:
                        final_state = await self._invoke_graph(run_id, state)
                    else:
                        final_state = await self._invoke_graph(
                            run_id,
                            state,
                            checkpoint_thread_id=checkpoint_thread_id,
                        )
                elif checkpoint_thread_id == run_id:
                    final_state = await self._invoke_graph(run_id, state, resume_payload=resume_payload)
                else:
                    final_state = await self._invoke_graph(
                        run_id,
                        state,
                        resume_payload=resume_payload,
                        checkpoint_thread_id=checkpoint_thread_id,
                    )
                self.observability.update_current_span(output={"status": "finished"})
            if resume_operator_request_id is not None:
                async with self.database.session() as session:
                    repo = Repository(session)
                    await repo.mark_operator_request_consumed(resume_operator_request_id)
            operator_interrupt = _extract_operator_interrupt(final_state)
            if operator_interrupt is not None:
                await self._record_operator_interrupt(operator_interrupt)
                return
            review = final_state.get("review", {})
            final_answer = final_state.get("final_answer", "")
            run_report = final_state.get("run_report") if isinstance(final_state.get("run_report"), dict) else None
            async with self.database.session() as session:
                repo = Repository(session)
                pending_approvals = await repo.count_approvals(run_id, status=ApprovalStatus.PENDING)
                blockers = list(review.get("blockers", []))
                plan_only = bool(final_state.get("plan_only")) or interaction_mode == InteractionMode.PLAN_ONLY.value
                if pending_approvals:
                    await repo.set_run_status(run_id, RunStatus.WAITING_APPROVAL, final_answer=final_answer)
                    log_event(
                        logger,
                        RunStatus.WAITING_APPROVAL.value,
                        run_id=run_id,
                        thread_id=thread_id,
                        trace_id=trace_id,
                        provider=model_provider,
                    )
                    await repo.add_thread_message(
                        thread_id,
                        author_type=ThreadMessageAuthorType.SYSTEM,
                        author_name="runtime",
                        message_type=ThreadMessageType.RUN_REPORT,
                        content="Run is waiting for approval.",
                        run_id=run_id,
                        metadata={"status": RunStatus.WAITING_APPROVAL.value, "run_report": run_report or {}},
                    )
                elif mode == RunMode.CODING.value and not plan_only and not review.get("can_proceed", False):
                    report = build_run_report(
                        {**final_state, "review": review},
                        status=RunStatus.FAILED_VERIFICATION.value,
                    )
                    report_artifact = await repo.add_artifact(run_id, "run_report", report.to_artifact())
                    run_report = {**report.to_artifact(), "artifact_id": report_artifact.id}
                    final_answer = report.chat_text()
                    await repo.set_run_status(run_id, RunStatus.FAILED_VERIFICATION, final_answer=final_answer)
                    log_event(
                        logger,
                        RunStatus.FAILED_VERIFICATION.value,
                        run_id=run_id,
                        thread_id=thread_id,
                        trace_id=trace_id,
                        provider=model_provider,
                    )
                    await repo.add_thread_message(
                        thread_id,
                        author_type=ThreadMessageAuthorType.AGENT,
                        author_name="reviewer",
                        message_type=ThreadMessageType.RUN_REPORT,
                        content=final_answer or "Run failed verification.",
                        run_id=run_id,
                        metadata={"status": RunStatus.FAILED_VERIFICATION.value, "run_report": run_report},
                    )
                else:
                    run_report = run_report or build_run_report(
                        final_state,
                        status=RunStatus.COMPLETED.value,
                    ).to_artifact()
                    await repo.set_run_status(run_id, RunStatus.COMPLETED, final_answer=final_answer)
                    await repo.add_event(run_id, EventType.RUN_COMPLETED.value, None, {})
                    log_event(
                        logger,
                        EventType.RUN_COMPLETED.value,
                        run_id=run_id,
                        thread_id=thread_id,
                        trace_id=trace_id,
                        provider=model_provider,
                    )
                    await repo.add_thread_message(
                        thread_id,
                        author_type=ThreadMessageAuthorType.AGENT,
                        author_name="synode",
                        message_type=ThreadMessageType.RUN_REPORT,
                        content=final_answer or "Run completed.",
                        run_id=run_id,
                        metadata={"status": RunStatus.COMPLETED.value, "run_report": run_report},
                    )
        except asyncio.CancelledError:
            await self._mark_run_cancelled(run_id, await self._cancellation_reason(run_id))
            raise
        except ApprovalRequired as exc:
            async with self.database.session() as session:
                repo = Repository(session)
                run = await repo.get_run(run_id)
                pending_approvals = await repo.count_approvals(run_id, status=ApprovalStatus.PENDING)
                if run is not None and run.status not in TERMINAL_RUN_STATUSES and pending_approvals:
                    await repo.set_run_status(run_id, RunStatus.WAITING_APPROVAL)
                log_event(
                    logger,
                    RunStatus.WAITING_APPROVAL.value,
                    run_id=run_id,
                    thread_id=run.thread_id if run is not None else None,
                    trace_id=trace_id,
                    provider=model_provider,
                    approval_id=exc.approval_id,
                    tool_name=exc.tool_name,
                )
        except OperatorRejected as exc:
            await self._mark_run_cancelled(run_id, str(exc))
        except Exception as exc:
            async with self.database.session() as session:
                repo = Repository(session)
                run = await repo.get_run(run_id)
                if run is not None and run.status == RunStatus.CANCELLED.value:
                    return
                await repo.set_run_status(run_id, RunStatus.FAILED, error=str(exc))
                await repo.add_event(run_id, EventType.RUN_FAILED.value, None, {"error": str(exc)})
                log_event(
                    logger,
                    EventType.RUN_FAILED.value,
                    run_id=run_id,
                    thread_id=run.thread_id if run is not None else None,
                    error_class=exc.__class__.__name__,
                    error=str(exc),
                )
                if run is not None:
                    await repo.add_thread_message(
                        run.thread_id,
                        author_type=ThreadMessageAuthorType.SYSTEM,
                        author_name="runtime",
                        message_type=ThreadMessageType.RUN_REPORT,
                        content=f"Run failed: {exc}",
                        run_id=run_id,
                        metadata={
                            "status": RunStatus.FAILED.value,
                            "run_report": build_run_report(
                                {
                                    "run_id": run_id,
                                    "thread_id": run.thread_id,
                                    "mode": run.mode,
                                    "review": {"blockers": [str(exc)]},
                                },
                                status=RunStatus.FAILED.value,
                            ).to_artifact(),
                        },
                    )
            raise

    async def _approved_native_patch_resume_id(self, run_id: str, mode: str) -> str | None:
        if mode != RunMode.CODING.value:
            return None
        async with self.database.session() as session:
            repo = Repository(session)
            if await repo.count_approvals(run_id, status=ApprovalStatus.PENDING):
                return None
            approvals = await repo.list_approvals(run_id=run_id, status=ApprovalStatus.APPROVED)
            audits = await repo.list_tool_audit(run_id)
        for approval in approvals:
            if approval.tool_name != "native.patch_apply":
                continue
            already_executed = any(
                audit.approval_id == approval.id and audit.status in {"ok", "error"}
                for audit in audits
            )
            if not already_executed:
                return approval.id
        return None

    async def _resume_native_coding_after_patch_approval(
        self,
        run_id: str,
        state: dict[str, Any],
    ) -> dict[str, Any]:
        roles = self._role_registry_for_state(state)
        tool_executor = ToolExecutor(
            self.database,
            roles,
            self.tools,
            self.settings,
            self.observability,
        )
        deps = GraphDependencies(
            database=self.database,
            roles=roles,
            models=self.models,
            tool_executor=tool_executor,
            observability=self.observability,
            secret_cipher=self.secret_cipher,
            execution_backends=self.execution_backends,
        )
        return await resume_native_coding_after_patch_approval(deps, state)

    async def approve(self, approval_id: str, reason: str | None = None) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            approval = await repo.decide_approval(approval_id, ApprovalStatus.APPROVED, reason)
            run = await repo.get_run(approval.run_id)
            if run is not None:
                await repo.add_thread_message(
                    run.thread_id,
                    author_type=ThreadMessageAuthorType.SYSTEM,
                    author_name="approval",
                    message_type=ThreadMessageType.APPROVAL_DECISION,
                    content=f"Approval approved for {approval.tool_name}.",
                    run_id=run.id,
                    metadata={"approval_id": approval.id, "status": ApprovalStatus.APPROVED.value},
                )

    async def _conversation_context(
        self,
        repo: Repository,
        thread_id: str,
        run_id: str,
    ) -> list[dict[str, Any]]:
        messages = await repo.latest_thread_messages(thread_id, limit=CONVERSATION_CONTEXT_SOURCE_LIMIT)
        selected: list[dict[str, Any]] = []
        remaining_chars = CONVERSATION_CONTEXT_CHAR_LIMIT
        for message in reversed(messages):
            if len(selected) >= CONVERSATION_CONTEXT_MESSAGE_LIMIT or remaining_chars <= 0:
                break
            if message.run_id == run_id:
                continue
            if message.message_type not in CONVERSATION_CONTEXT_MESSAGE_TYPES:
                continue
            content = " ".join(message.content.split())
            if not content:
                continue
            if len(content) > remaining_chars:
                content = content[:remaining_chars].rstrip() + " ...[truncated]"
            selected.append(
                {
                    "author_type": message.author_type,
                    "author_name": message.author_name,
                    "message_type": message.message_type,
                    "content": content,
                }
            )
            remaining_chars -= len(content)
        return list(reversed(selected))

    async def reject(self, approval_id: str, reason: str | None = None) -> None:
        rejected_payload: dict[str, Any] = {}
        async with self.database.session() as session:
            repo = Repository(session)
            approval = await repo.decide_approval(approval_id, ApprovalStatus.REJECTED, reason)
            rejected_payload = approval.payload or {}
            run = await repo.get_run(approval.run_id)
            if run is not None:
                await repo.add_thread_message(
                    run.thread_id,
                    author_type=ThreadMessageAuthorType.SYSTEM,
                    author_name="approval",
                    message_type=ThreadMessageType.APPROVAL_DECISION,
                    content=f"Approval rejected for {approval.tool_name}.",
                    run_id=run.id,
                    metadata={"approval_id": approval.id, "status": ApprovalStatus.REJECTED.value},
                )
        try:
            await self.execution_backends.reject_approval(
                rejected_payload,
                reason or f"Approval rejected: {approval_id}",
            )
        except Exception as exc:
            log_event(
                logger,
                "external_runtime_reject_failed",
                approval_id=approval_id,
                error_class=exc.__class__.__name__,
                error=str(exc),
            )
        await self._mark_run_cancelled(approval.run_id, f"Approval rejected for {approval.tool_name}.")

    async def resume_run(self, run_id: str) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                raise ValueError(f"run is terminal: {run_id}")
        await self.start_run(run_id)

    async def stop_run(self, run_id: str, reason: str | None = None) -> RunResponse:
        stop_reason = reason or "Run stopped by user."
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                return to_run_response(run)
            if run.status == RunStatus.CANCELLING.value:
                return to_run_response(run)
            if run.status == RunStatus.RUNNING.value and run_id not in self._run_tasks:
                await repo.request_run_cancellation(run_id, stop_reason)
                return to_run_response(run)
        task = self._run_tasks.get(run_id)
        if task is not None and not task.done():
            self._stop_reasons[run_id] = stop_reason
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        else:
            await self._mark_run_cancelled(run_id, stop_reason)
        return await self.get_run(run_id)

    async def _mark_run_cancelled(self, run_id: str, reason: str) -> None:
        should_cancel_external = False
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                return
            should_cancel_external = True
            await repo.reject_pending_approvals(run_id, reason)
            await repo.cancel_pending_operator_requests(run_id, reason)
            await repo.set_run_status(run_id, RunStatus.CANCELLED, final_answer=reason, error=reason)
            await repo.add_event(run_id, EventType.RUN_CANCELLED.value, None, {"reason": reason})
            log_event(
                logger,
                EventType.RUN_CANCELLED.value,
                run_id=run_id,
                thread_id=run.thread_id,
                error_class="CancelledError",
                error=reason,
            )
            await repo.add_thread_message(
                run.thread_id,
                author_type=ThreadMessageAuthorType.SYSTEM,
                author_name="runtime",
                message_type=ThreadMessageType.RUN_REPORT,
                content=f"Run cancelled: {reason}",
                run_id=run_id,
                metadata={
                    "status": RunStatus.CANCELLED.value,
                    "run_report": build_run_report(
                        {
                            "run_id": run_id,
                            "thread_id": run.thread_id,
                            "mode": run.mode,
                            "review": {"blockers": [reason]},
                        },
                        status=RunStatus.CANCELLED.value,
                    ).to_artifact(),
                },
            )
        if should_cancel_external:
            with suppress(Exception):
                await self.execution_backends.cancel_run(run_id)

    async def _cancellation_reason(self, run_id: str) -> str:
        reason = self._stop_reasons.pop(run_id, None)
        if reason:
            return reason
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is not None and run.error:
                return run.error
        return "Run stopped by user."

    async def claim_queued_run(self, run_id: str, worker_id: str) -> RunResponse | None:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.claim_queued_run(run_id, worker_id)
            return to_run_response(run) if run is not None else None

    async def heartbeat_run(self, run_id: str, worker_id: str) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            await repo.heartbeat_run(run_id, worker_id)

    async def record_worker_heartbeat(
        self,
        *,
        worker_id: str,
        hostname: str,
        pid: int,
        status: str,
        current_run_id: str | None,
        started_at: datetime,
    ) -> WorkerHeartbeatResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            record = await repo.upsert_worker_heartbeat(
                worker_id=worker_id,
                hostname=hostname,
                pid=pid,
                status=status,
                current_run_id=current_run_id,
                started_at=started_at,
            )
            return WorkerHeartbeatResponse(
                worker_id=record.worker_id,
                hostname=record.hostname,
                pid=record.pid,
                status=record.status,
                current_run_id=record.current_run_id,
                started_at=record.started_at,
                heartbeat_at=record.heartbeat_at,
            )

    async def recover_stale_runs(self) -> dict[str, Any]:
        stale_before = datetime.now(UTC) - timedelta(seconds=self.settings.worker_stale_after_seconds)
        async with self.database.session() as session:
            repo = Repository(session)
            return await repo.recover_stale_runs(stale_before)

    async def reconcile_run_queue(self) -> int:
        async with self.database.session() as session:
            repo = Repository(session)
            run_ids = await repo.list_queued_run_ids(limit=self.settings.db_row_limit)
        return await self.run_queue.reconcile_runs(run_ids)

    async def runtime_status(self) -> RuntimeStatusResponse:
        stale_before = datetime.now(UTC) - timedelta(seconds=self.settings.worker_stale_after_seconds)
        async with self.database.session() as session:
            repo = Repository(session)
            heartbeats = await repo.list_worker_heartbeats()
            queue_depth = await repo.count_runs(RunStatus.QUEUED)
            running_count = await repo.count_runs(RunStatus.RUNNING)
            cancelling_count = await repo.count_runs(RunStatus.CANCELLING)
            stale_running_count = await repo.count_stale_running_runs(stale_before)
        queue = await self.run_queue.status()
        execution_backends = await self.execution_backends.statuses()
        return RuntimeStatusResponse(
            queue_depth=queue_depth,
            running_count=running_count,
            cancelling_count=cancelling_count,
            stale_running_count=stale_running_count,
            worker_concurrency=self.settings.worker_concurrency,
            secrets_configured=bool(self.settings.secrets_key and self.settings.secrets_key.strip()),
            queue=QueueStatusResponse(
                backend=queue.backend,
                available=queue.available,
                detail=queue.detail,
                queue_name=queue.queue_name,
                pending_jobs=queue.pending_jobs,
                running_jobs=queue.running_jobs,
                failed_jobs=queue.failed_jobs,
            ),
            execution_backends={
                backend: ExecutionBackendStatusResponse(
                    backend=status.backend,
                    available=status.available,
                    detail=status.detail,
                )
                for backend, status in execution_backends.items()
            },
            workers=[
                WorkerHeartbeatResponse(
                    worker_id=heartbeat.worker_id,
                    hostname=heartbeat.hostname,
                    pid=heartbeat.pid,
                    status=heartbeat.status,
                    current_run_id=heartbeat.current_run_id,
                    started_at=heartbeat.started_at,
                    heartbeat_at=heartbeat.heartbeat_at,
                )
                for heartbeat in heartbeats
            ],
            sandbox=self.sandbox_status(),
        )

    def sandbox_status(self) -> SandboxStatusResponse:
        status = SandboxRunner(self.settings).status()
        return SandboxStatusResponse(
            backend=status.backend,
            available=status.available,
            detail=status.detail,
            cpu_seconds=status.cpu_seconds,
            memory_mb=status.memory_mb,
            disk_mb=status.disk_mb,
            output_max_bytes=status.output_max_bytes,
        )

    async def cleanup_retention(self) -> dict[str, int]:
        async with self.database.session() as session:
            repo = Repository(session)
            return await repo.cleanup_retention(
                run_event_days=self.settings.run_event_retention_days,
                model_delta_days=self.settings.model_delta_retention_days,
                tool_audit_days=self.settings.tool_audit_retention_days,
                artifact_days=self.settings.artifact_retention_days,
                archived_thread_days=self.settings.archived_thread_retention_days,
            )

    async def model_health(self, limit: int = 50, offset: int = 0) -> list[dict[str, object]]:
        async with self.database.session() as session:
            repo = Repository(session)
            await self._ensure_default_configuration(repo)
            profiles = await repo.list_model_profiles(limit=limit, offset=offset)
            results: list[dict[str, object]] = []
            for profile in profiles:
                item = {
                    "profile_id": profile.id,
                    "profile_name": profile.name,
                    "provider_type": profile.provider_type,
                    "provider": profile.provider_type,
                    "model": profile.model,
                    "ok": False,
                    "error": None,
                }
                if not profile.enabled:
                    item["error"] = "profile is disabled"
                    results.append(item)
                    continue
                try:
                    provider = await self._provider_for_profile(repo, profile)
                    health = await provider.health()
                    item.update(health.model_dump(mode="json"))
                except Exception as exc:
                    item["error"] = str(exc)
                results.append(item)
            return results

    async def list_secrets(self, limit: int = 50, offset: int = 0) -> list[SecretResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            return [to_secret_response(secret) for secret in await repo.list_secrets(limit=limit, offset=offset)]

    async def create_secret(self, payload: SecretCreateRequest) -> SecretResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            secret = await repo.create_secret(payload.name, self._cipher().encrypt(payload.value))
            return to_secret_response(secret)

    async def update_secret(self, secret_id: str, payload: SecretUpdateRequest) -> SecretResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            secret = await repo.update_secret(secret_id, self._cipher().encrypt(payload.value))
            return to_secret_response(secret)

    async def list_model_profiles(self, limit: int = 50, offset: int = 0) -> list[ModelProfileResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            await self._ensure_default_configuration(repo)
            return [
                to_model_profile_response(profile)
                for profile in await repo.list_model_profiles(limit=limit, offset=offset)
            ]

    async def create_model_profile(self, payload: ModelProfileCreateRequest) -> ModelProfileResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            profile = await repo.create_model_profile(
                name=payload.name,
                provider_type=payload.provider_type,
                base_url=payload.base_url,
                model=payload.model,
                options=payload.options,
                secret_id=payload.secret_id,
                enabled=payload.enabled,
            )
            return to_model_profile_response(profile)

    async def update_model_profile(
        self,
        profile_id: str,
        payload: ModelProfileUpdateRequest,
    ) -> ModelProfileResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            profile = await repo.update_model_profile(
                profile_id,
                payload.model_dump(exclude_unset=True),
            )
            return to_model_profile_response(profile)

    async def list_mcp_servers(self, limit: int = 50, offset: int = 0) -> list[MCPServerResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            return [
                to_mcp_server_response(server)
                for server in await repo.list_mcp_servers(limit=limit, offset=offset)
            ]

    async def create_mcp_server(self, payload: MCPServerCreateRequest) -> MCPServerResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            server = await repo.create_mcp_server(
                name=payload.name,
                transport=payload.transport,
                config=self._normalize_mcp_config(payload.transport, payload.config),
                enabled=payload.enabled,
            )
            response = to_mcp_server_response(server)
        await self.refresh_mcp_tools()
        return response

    async def update_mcp_server(self, server_id: str, payload: MCPServerUpdateRequest) -> MCPServerResponse:
        values = payload.model_dump(exclude_unset=True)
        if "transport" in values or "config" in values:
            transport = values.get("transport")
            config = values.get("config")
            if transport is None or config is None:
                async with self.database.session() as session:
                    repo = Repository(session)
                    current = await repo.get_mcp_server(server_id)
                    if current is None:
                        raise LookupError(f"MCP server not found: {server_id}")
                    if transport is None:
                        transport = MCPServerTransport(current.transport)
                    if config is None:
                        config = current.config or {}
            values["config"] = self._normalize_mcp_config(
                MCPServerTransport(transport),
                config,
            )
        async with self.database.session() as session:
            repo = Repository(session)
            server = await repo.update_mcp_server(server_id, values)
            response = to_mcp_server_response(server)
        await self.refresh_mcp_tools()
        return response

    async def delete_mcp_server(self, server_id: str) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            await repo.delete_mcp_server(server_id)
        await self.refresh_mcp_tools()

    async def discover_mcp_server(self, server_id: str) -> MCPServerResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            server = await repo.get_mcp_server(server_id)
            if server is None:
                raise LookupError(f"MCP server not found: {server_id}")
            server_name = server.name
            config = server.config or {}
        try:
            tools = await discover_mcp_tools(server_name, config)
        except Exception as exc:
            async with self.database.session() as session:
                repo = Repository(session)
                server = await repo.record_mcp_discovery(server_id, error=str(exc))
                response = to_mcp_server_response(server)
            await self.refresh_mcp_tools()
            raise RuntimeError(str(exc)) from exc
        async with self.database.session() as session:
            repo = Repository(session)
            server = await repo.record_mcp_discovery(server_id, tools=tools)
            response = to_mcp_server_response(server)
        await self.refresh_mcp_tools()
        return response

    async def refresh_mcp_tools(self) -> None:
        self.tools.unregister_prefix("mcp.")
        async with self.database.session() as session:
            repo = Repository(session)
            servers = await repo.list_mcp_servers(enabled_only=True, limit=1000)
            runtime_configs = [
                MCPServerRuntimeConfig(
                    name=server.name,
                    config=server.config or {},
                    tools=list(server.tools or []),
                )
                for server in servers
                if server.tools
            ]
        register_mcp_tools(self.tools, runtime_configs)

    async def handle_mcp_proxy_request(
        self,
        *,
        session_id: str,
        authorization: str | None,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        token = _bearer_token(authorization)
        return await self.tool_executor.handle_mcp_proxy_request(
            session_id=session_id,
            token=token,
            payload=payload,
        )

    async def test_model_profile(self, profile_id: str) -> ModelProfileTestResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            profile = await repo.get_model_profile(profile_id)
            if profile is None:
                raise LookupError(f"model profile not found: {profile_id}")
            if not profile.enabled:
                return ModelProfileTestResponse(
                    profile_id=profile.id,
                    ok=False,
                    provider_type=ModelProviderType(profile.provider_type),
                    model=profile.model,
                    capabilities=ModelProfileTestCapabilities(streaming=False, structured_output=False),
                    checks=[
                        ModelProfileTestCheck(
                            name="health",
                            ok=False,
                            supported=True,
                            error="profile is disabled",
                        )
                    ],
                )

            provider = await self._provider_for_profile(repo, profile)
            checks: list[ModelProfileTestCheck] = []

            started = time.perf_counter()
            health = await provider.health()
            checks.append(
                ModelProfileTestCheck(
                    name="health",
                    ok=health.ok,
                    supported=True,
                    latency_ms=(time.perf_counter() - started) * 1000,
                    error=health.error,
                )
            )

            started = time.perf_counter()
            try:
                await provider.invoke(
                    ModelRequest(
                        role="model_profile_probe",
                        prompt="Return a small JSON probe response.",
                        response_schema=ModelProfileStructuredProbe,
                        temperature=0.0,
                        timeout_seconds=15,
                    )
                )
                checks.append(
                    ModelProfileTestCheck(
                        name="structured_output",
                        ok=True,
                        supported=True,
                        latency_ms=(time.perf_counter() - started) * 1000,
                    )
                )
            except Exception as exc:
                checks.append(
                    ModelProfileTestCheck(
                        name="structured_output",
                        ok=False,
                        supported=True,
                        latency_ms=(time.perf_counter() - started) * 1000,
                        error=str(exc),
                    )
                )

            supports_streaming = bool(getattr(provider, "supports_streaming", False))
            invoke_stream: Any = getattr(provider, "invoke_stream", None)
            if supports_streaming and not callable(invoke_stream):
                checks.append(
                    ModelProfileTestCheck(
                        name="streaming",
                        ok=False,
                        supported=True,
                        error=f"provider {provider.name} advertises streaming without invoke_stream",
                    )
                )
            elif not supports_streaming:
                checks.append(ModelProfileTestCheck(name="streaming", ok=True, supported=False))
            else:
                started = time.perf_counter()

                async def ignore_delta(_delta: str) -> None:
                    return None

                try:
                    await invoke_stream(
                        ModelRequest(
                            role="model_profile_probe",
                            prompt="Reply with the word ok.",
                            temperature=0.0,
                            timeout_seconds=15,
                        ),
                        ignore_delta,
                    )
                    checks.append(
                        ModelProfileTestCheck(
                            name="streaming",
                            ok=True,
                            supported=True,
                            latency_ms=(time.perf_counter() - started) * 1000,
                        )
                    )
                except Exception as exc:
                    checks.append(
                        ModelProfileTestCheck(
                            name="streaming",
                            ok=False,
                            supported=True,
                            latency_ms=(time.perf_counter() - started) * 1000,
                            error=str(exc),
                        )
                    )

            structured_ok = any(check.name == "structured_output" and check.ok for check in checks)
            streaming_ok = any(check.name == "streaming" and check.ok and check.supported for check in checks)
            failed_supported_checks = [check for check in checks if check.supported and not check.ok]
            return ModelProfileTestResponse(
                profile_id=profile.id,
                ok=not failed_supported_checks,
                provider_type=ModelProviderType(profile.provider_type),
                model=profile.model,
                capabilities=ModelProfileTestCapabilities(
                    streaming=streaming_ok,
                    structured_output=structured_ok,
                ),
                checks=checks,
            )

    async def list_agent_roles(self, limit: int = 100, offset: int = 0) -> list[AgentRoleResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            await self._ensure_default_configuration(repo)
            return [to_agent_role_response(role) for role in await repo.list_agent_roles(limit=limit, offset=offset)]

    async def create_agent_role(self, payload: AgentRoleCreateRequest) -> AgentRoleResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            role = await repo.create_agent_role(
                name=payload.name,
                mission=payload.mission,
                non_goals=payload.non_goals,
                allowed_tools=payload.allowed_tools,
                requires_approval_for=payload.requires_approval_for,
                output_contract=payload.output_contract,
                builtin=False,
                enabled=payload.enabled,
            )
            return to_agent_role_response(role)

    async def update_agent_role(self, role_id: str, payload: AgentRoleUpdateRequest) -> AgentRoleResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            role = await repo.update_agent_role(role_id, payload.model_dump(exclude_unset=True))
            return to_agent_role_response(role)

    async def list_agent_graphs(self, limit: int = 50, offset: int = 0) -> list[AgentGraphResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            await self._ensure_default_configuration(repo)
            return [to_agent_graph_response(graph) for graph in await repo.list_agent_graphs(limit=limit, offset=offset)]

    async def create_agent_graph(self, payload: AgentGraphCreateRequest) -> AgentGraphResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            graph = await repo.create_agent_graph(
                name=payload.name,
                nodes=[node.model_dump(mode="json") for node in payload.nodes],
                node_edges=[edge.model_dump(mode="json") for edge in payload.node_edges],
                graph_schema_version=payload.graph_schema_version,
                default_model_profile_id=payload.default_model_profile_id,
                role_model_profile_ids=payload.role_model_profile_ids,
                node_runtime_bindings=payload.node_runtime_bindings,
                node_contracts=payload.node_contracts,
                is_default=payload.is_default,
                enabled=payload.enabled,
            )
            return to_agent_graph_response(graph)

    async def update_agent_graph(
        self,
        graph_id: str,
        payload: AgentGraphUpdateRequest,
    ) -> AgentGraphResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            values = payload.model_dump(exclude_unset=True)
            if "nodes" in values:
                values["nodes"] = [node.model_dump(mode="json") for node in payload.nodes or []]
            if "node_edges" in values:
                values["node_edges"] = [edge.model_dump(mode="json") for edge in payload.node_edges or []]
            graph = await repo.update_agent_graph(graph_id, values)
            return to_agent_graph_response(graph)

    async def run_metrics(self, run_id: str) -> RunMetricsResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            event_count = await repo.count_events(run_id)
            model_events = await repo.model_invocation_events(run_id)
            tool_call_count = await repo.count_tool_audit(run_id)
            failed_tool_call_count = await repo.count_tool_audit(run_id, statuses={"denied", "error"})
            approval_count = await repo.count_approvals(run_id)
            pending_approval_count = await repo.count_approvals(run_id, status=ApprovalStatus.PENDING)

        token_usage = _sum_token_usage(event.payload.get("usage", {}) for event in model_events)
        provider_usage: dict[str, TokenUsage] = {}
        latency_ms_by_role: dict[str, float] = {}
        for event in model_events:
            provider = str(event.payload.get("provider") or "unknown")
            provider_usage[provider] = _add_token_usage(
                provider_usage.get(provider),
                event.payload.get("usage", {}),
            )
            role = str(event.payload.get("role") or event.role or "unknown")
            latency_ms = event.payload.get("latency_ms")
            if isinstance(latency_ms, int | float):
                latency_ms_by_role[role] = latency_ms_by_role.get(role, 0.0) + float(latency_ms)

        duration_ms = (run.updated_at - run.created_at).total_seconds() * 1000 if run.updated_at else None
        return RunMetricsResponse(
            run_id=run_id,
            status=RunStatus(run.status),
            duration_ms=duration_ms,
            event_count=event_count,
            model_call_count=len(model_events),
            tool_call_count=tool_call_count,
            approval_count=approval_count,
            pending_approval_count=pending_approval_count,
            failed_tool_call_count=failed_tool_call_count,
            token_usage=token_usage,
            provider_usage=provider_usage,
            latency_ms_by_role=latency_ms_by_role,
        )

    async def system_metrics(self) -> SystemMetricsResponse:
        try:
            import psutil
        except ImportError as exc:
            raise RuntimeError("psutil is required for system metrics") from exc

        process = psutil.Process(os.getpid())
        with process.oneshot():
            process_metrics = ProcessMetrics(
                pid=process.pid,
                uptime_seconds=max(0.0, time.time() - process.create_time()),
                cpu_percent=float(process.cpu_percent(interval=None)),
                memory_rss_bytes=int(process.memory_info().rss),
                memory_percent=float(process.memory_percent()),
            )
        return SystemMetricsResponse(process=process_metrics, gpu=await _gpu_metrics())

    async def ensure_default_configuration(self) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            await self._ensure_default_configuration(repo)

    async def _ensure_default_configuration(self, repo: Repository) -> None:
        await repo.ensure_default_configuration(
            builtin_roles=self.roles.as_public(),
            ollama_base_url=self.settings.ollama_base_url,
            ollama_model=self.settings.ollama_model,
        )

    async def _prepare_run_config(
        self,
        repo: Repository,
        mode: RunMode,
        model_provider: str | None,
        default_model_profile_id: str | None,
        role_model_profile_ids: dict[str, str],
        agent_graph_id: str | None,
    ) -> dict[str, Any]:
        await self._ensure_default_configuration(repo)
        graph = await repo.get_agent_graph(agent_graph_id) if agent_graph_id else await repo.get_default_agent_graph()
        if graph is None:
            raise LookupError("agent graph not found")
        if not graph.enabled:
            raise ValueError(f"agent graph is disabled: {graph.name}")

        snapshot, roles_by_id, roles_by_name = await self._snapshot_graph(repo, graph)
        role_bindings = await self._resolve_role_model_bindings(
            repo,
            roles_by_id,
            roles_by_name,
            {
                **(graph.role_model_profile_ids or {}),
                **role_model_profile_ids,
            },
        )
        node_contracts = self._resolve_node_contracts(snapshot, graph.node_contracts or {})
        node_runtime_bindings = self._resolve_node_runtime_bindings(
            snapshot,
            graph.node_runtime_bindings or {},
        )
        for node_id, backend in node_runtime_bindings.items():
            validate_backend_contract(backend, node_contracts[node_id])
        for node in snapshot["nodes"]:
            node_id = str(node["id"])
            node["runtime_backend"] = node_runtime_bindings[node_id]
            node["contract_id"] = node_contracts[node_id]
        snapshot["node_runtime_bindings"] = node_runtime_bindings
        snapshot["node_contracts"] = node_contracts
        if RuntimeBackend.OPENHANDS.value in node_runtime_bindings.values() and not self.settings.openhands_enabled:
            raise ValueError("agent graph requires OpenHands but SYNODE_OPENHANDS_ENABLED is false")
        profile_id = None
        if default_model_profile_id is not None:
            profile_id = default_model_profile_id
        elif model_provider is None:
            profile_id = graph.default_model_profile_id

        provider_label = model_provider or self.settings.model_provider
        if profile_id is not None:
            profile = await repo.get_model_profile(profile_id)
            if profile is None:
                raise LookupError(f"model profile not found: {profile_id}")
            if not profile.enabled:
                raise ValueError(f"model profile is disabled: {profile.name}")
            provider_label = profile.provider_type

        role_names = {role["name"] for role in snapshot["roles"]}
        required = {RoleName.SUPERVISOR.value, RoleName.REVIEWER.value}
        if mode == RunMode.CODING:
            required.add(RoleName.CODER.value)
        missing = required - role_names
        if missing:
            raise ValueError(f"agent graph is missing required roles for {mode.value}: {sorted(missing)}")

        return {
            "model_provider": provider_label,
            "default_model_profile_id": profile_id,
            "role_model_profile_ids": role_bindings,
            "agent_graph_id": graph.id,
            "agent_graph_snapshot": snapshot,
        }

    async def _snapshot_graph(
        self,
        repo: Repository,
        graph: Any,
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        roles_by_id: dict[str, Any] = {}
        roles_by_name: dict[str, Any] = {}
        roles: list[dict[str, Any]] = []
        nodes: list[dict[str, Any]] = []
        for raw_node in graph.nodes or []:
            if not isinstance(raw_node, dict):
                continue
            role_id = str(raw_node.get("role_id"))
            role = await repo.get_agent_role(role_id)
            if role is None:
                raise LookupError(f"agent role not found: {role_id}")
            if not role.enabled:
                continue
            roles_by_id[role.id] = role
            roles_by_name[role.name] = role
            roles.append(
                {
                    "id": role.id,
                    "name": role.name,
                    "mission": role.mission,
                    "non_goals": role.non_goals or [],
                    "allowed_tools": role.allowed_tools or [],
                    "requires_approval_for": role.requires_approval_for or [],
                    "output_contract": role.output_contract,
                    "builtin": role.builtin,
                }
            )
            nodes.append(
                {
                    "id": str(raw_node["id"]),
                    "role_id": role.id,
                    "role": role.name,
                    "label": str(raw_node.get("label") or role.name),
                    "kind": str(raw_node.get("kind") or "worker"),
                    "output_contract": role.output_contract,
                }
            )
        if not nodes:
            raise ValueError("agent graph nodes are required")
        node_by_id = {str(node["id"]): node for node in nodes}
        node_edges: list[dict[str, str]] = []
        for raw_edge in graph.node_edges or []:
            source = node_by_id.get(str(raw_edge.get("from_node")))
            target = node_by_id.get(str(raw_edge.get("to_node")))
            if source is None or target is None:
                raise ValueError("agent graph node_edges must reference enabled graph nodes")
            node_edges.append({"from_node": str(source["id"]), "to_node": str(target["id"])})
        return (
            {
                "id": graph.id,
                "name": graph.name,
                "graph_schema_version": graph.graph_schema_version,
                "roles": roles,
                "nodes": nodes,
                "node_edges": node_edges,
            },
            roles_by_id,
            roles_by_name,
        )

    async def _resolve_role_model_bindings(
        self,
        repo: Repository,
        roles_by_id: dict[str, Any],
        roles_by_name: dict[str, Any],
        bindings: dict[str, str],
    ) -> dict[str, str]:
        resolved: dict[str, str] = {}
        for key, profile_id in bindings.items():
            role = roles_by_id.get(key) or roles_by_name.get(key)
            if role is None:
                raise LookupError(f"agent role not found in selected graph: {key}")
            profile = await repo.get_model_profile(profile_id)
            if profile is None:
                raise LookupError(f"model profile not found: {profile_id}")
            if not profile.enabled:
                raise ValueError(f"model profile is disabled: {profile.name}")
            resolved[role.name] = profile.id
        return resolved

    def _resolve_node_runtime_bindings(
        self,
        snapshot: dict[str, Any],
        node_bindings: dict[str, Any],
    ) -> dict[str, str]:
        nodes = snapshot.get("nodes", [])
        node_ids = {str(node["id"]) for node in nodes if isinstance(node, dict) and node.get("id")}
        resolved = {node_id: RuntimeBackend.NATIVE_LANGGRAPH.value for node_id in node_ids}
        for node_id, backend in node_bindings.items():
            if str(node_id) not in resolved:
                raise LookupError(f"agent graph node not found: {node_id}")
            resolved[str(node_id)] = self._runtime_backend_id(backend)
        return resolved

    def _resolve_node_contracts(self, snapshot: dict[str, Any], node_contracts: dict[str, Any]) -> dict[str, str]:
        registry = default_contract_registry()
        nodes = snapshot.get("nodes", [])
        node_ids = {str(node["id"]) for node in nodes if isinstance(node, dict) and node.get("id")}
        unknown = set(node_contracts) - node_ids
        if unknown:
            raise LookupError(f"agent graph node not found: {sorted(unknown)}")
        resolved: dict[str, str] = {}
        for node in nodes:
            node_id = str(node["id"])
            role_name = str(node["role"])
            contract_id = str(node_contracts.get(node_id) or default_contract_for_role(role_name))
            registry.validate_binding(contract_id, role_name=role_name, node_kind=str(node["kind"]))
            resolved[node_id] = contract_id
        return resolved

    def _runtime_backend_id(self, backend: Any) -> str:
        value = backend.value if isinstance(backend, RuntimeBackend) else str(backend)
        if value not in self.execution_backends.known_backend_ids():
            raise ValueError(f"unknown runtime backend: {value}")
        return value

    async def _provider_for_profile(self, repo: Repository, profile: Any) -> Any:
        api_key = None
        if profile.secret_id:
            secret = await repo.get_secret(profile.secret_id)
            if secret is None:
                raise LookupError(f"secret not found: {profile.secret_id}")
            api_key = self._cipher().decrypt(secret.encrypted_value)
        return self.models.for_profile(profile, api_key)

    def _cipher(self) -> SecretCipher:
        if self.secret_cipher is None:
            raise RuntimeError("SYNODE_SECRETS_KEY is required for DB secrets")
        return self.secret_cipher

    @staticmethod
    def _normalize_mcp_config(transport: MCPServerTransport, config: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(config)
        normalized["transport"] = transport.value
        if transport == MCPServerTransport.STDIO:
            command = str(normalized.get("command") or "").strip()
            if not command:
                raise ValueError("stdio MCP server config requires command")
            args = normalized.get("args", [])
            if args is None:
                args = []
            if not isinstance(args, list) or not all(isinstance(item, str) for item in args):
                raise ValueError("stdio MCP server config args must be a list of strings")
            normalized["args"] = args
        else:
            url = str(normalized.get("url") or "").strip()
            if not url:
                raise ValueError(f"{transport.value} MCP server config requires url")
        return normalized

    async def _record_operator_interrupt(self, operator_interrupt: OperatorInterrupt) -> None:
        payload = operator_interrupt.payload
        run_id = str(payload.get("run_id") or "")
        if not run_id:
            raise RuntimeError("operator interrupt is missing run_id")
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            thread_id = str(payload.get("thread_id") or run.thread_id)
            if thread_id != run.thread_id:
                raise RuntimeError("operator interrupt thread_id does not match run")
            kind = OperatorRequestKind(str(payload.get("kind") or ""))
            role = _optional_string(payload.get("role"))
            await repo.create_operator_request(
                request_id=operator_interrupt.interrupt_id,
                run_id=run.id,
                thread_id=run.thread_id,
                node_id=_optional_string(payload.get("node_id")),
                role=role,
                kind=kind,
                prompt=str(payload.get("prompt") or "Operator input required."),
                context=_dict_or_empty(payload.get("context")),
                proposed_payload=_dict_or_empty(payload.get("proposed_payload")),
            )
            log_event(
                logger,
                EventType.OPERATOR_REQUIRED.value,
                run_id=run.id,
                thread_id=run.thread_id,
                trace_id=run.observability_trace_id,
                role=role,
                operator_request_id=operator_interrupt.interrupt_id,
                kind=kind.value,
            )

    async def _invoke_graph(
        self,
        run_id: str,
        state: dict[str, Any],
        *,
        resume_payload: dict[str, Any] | None = None,
        checkpoint_thread_id: str | None = None,
    ) -> dict[str, Any]:
        roles = self._role_registry_for_state(state)
        tool_executor = ToolExecutor(
            self.database,
            roles,
            self.tools,
            self.settings,
            self.observability,
        )
        deps = GraphDependencies(
            database=self.database,
            roles=roles,
            models=self.models,
            tool_executor=tool_executor,
            observability=self.observability,
            secret_cipher=self.secret_cipher,
            execution_backends=self.execution_backends,
        )
        async with self._checkpointer() as checkpointer:
            graph = build_graph(deps, checkpointer=checkpointer)
            graph_input: Any = Command(resume=resume_payload) if resume_payload is not None else state
            result = await graph.ainvoke(
                graph_input,
                config={"configurable": {"thread_id": checkpoint_thread_id or run_id}},
            )
            return dict(result)

    def _role_registry_for_state(self, state: dict[str, Any]) -> RoleRegistry:
        snapshot = state.get("agent_graph_snapshot") or {}
        raw_roles = snapshot.get("roles", [])
        if not isinstance(raw_roles, list) or not raw_roles:
            return self.roles
        specs = []
        for role in raw_roles:
            if not isinstance(role, dict):
                continue
            specs.append(
                RoleSpec(
                    name=str(role["name"]),
                    mission=str(role["mission"]),
                    non_goals=list(role.get("non_goals", [])),
                    allowed_tools=list(role.get("allowed_tools", [])),
                    requires_approval_for=list(role.get("requires_approval_for", [])),
                    output_contract=str(role.get("output_contract", "")),
                )
            )
        if not specs:
            return self.roles
        return RoleRegistry.from_specs(specs)

    @asynccontextmanager
    async def _checkpointer(self) -> AsyncIterator[Any]:
        if self.settings.enable_postgres_checkpointer and self.settings.checkpoint_database_url.startswith(
            "postgresql"
        ):
            os.environ.setdefault("LANGGRAPH_POSTGRES_POOL_MAX_SIZE", "5")
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

            async with AsyncPostgresSaver.from_conn_string(
                self.settings.checkpoint_database_url
            ) as checkpointer:
                await checkpointer.setup()
                yield checkpointer
        else:
            yield self._memory_checkpointer

    async def close(self) -> None:
        for task in list(self._run_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._run_tasks.values()):
            with suppress(asyncio.CancelledError):
                await task
        self._run_tasks.clear()
        await self.run_queue.close()
        await self.database.close()
        self.observability.shutdown()

    async def _thread_detail(
        self,
        repo: Repository,
        thread_id: str,
        runs_limit: int = 50,
        runs_offset: int = 0,
        messages_limit: int = 200,
        messages_offset: int = 0,
    ) -> ThreadDetailResponse:
        thread = await repo.get_thread(thread_id)
        if thread is None:
            raise LookupError(f"thread not found: {thread_id}")
        runs = await repo.list_thread_runs(thread_id, limit=runs_limit, offset=runs_offset)
        messages = await repo.list_thread_messages(thread_id, limit=messages_limit, offset=messages_offset)
        latest_run = runs[0] if runs else None
        latest_message = messages[-1] if messages else None
        return ThreadDetailResponse(
            thread=to_thread_response(thread, latest_run=latest_run, latest_message=latest_message),
            runs=[to_run_response(run) for run in runs],
            messages=[to_thread_message_response(message) for message in messages],
        )


async def create_service(settings: Settings, include_mcp: bool = True) -> OrchestrationService:
    settings.validate_startup()
    database = Database(settings)
    roles = RoleRegistry.load_builtin()
    models = ModelProviderRegistry(settings)
    observability = Observability(settings)
    tools = await build_tool_registry(settings, include_mcp=include_mcp)
    run_queue = build_run_queue_transport(settings)
    service = OrchestrationService(settings, database, roles, models, tools, observability, run_queue)
    try:
        await run_queue.open()
        await service.ensure_default_configuration()
        if include_mcp:
            await service.refresh_mcp_tools()
        return service
    except Exception:
        await service.close()
        raise


def _sum_token_usage(items: Any) -> TokenUsage:
    total = TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0)
    saw_any = False
    for item in items:
        total = _add_token_usage(total, item)
        if isinstance(item, dict) and any(
            isinstance(item.get(key), int) and not isinstance(item.get(key), bool)
            for key in ("input_tokens", "output_tokens", "total_tokens")
        ):
            saw_any = True
    if not saw_any:
        return TokenUsage()
    return total


def _add_token_usage(existing: TokenUsage | None, item: Any) -> TokenUsage:
    current = existing or TokenUsage(input_tokens=0, output_tokens=0, total_tokens=0)
    if not isinstance(item, dict):
        return current
    return TokenUsage(
        input_tokens=_sum_optional(current.input_tokens, item.get("input_tokens")),
        output_tokens=_sum_optional(current.output_tokens, item.get("output_tokens")),
        total_tokens=_sum_optional(current.total_tokens, item.get("total_tokens")),
    )


def _bearer_token(authorization: str | None) -> str:
    if not authorization:
        return ""
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return ""
    return token.strip()


def _extract_operator_interrupt(result: dict[str, Any]) -> OperatorInterrupt | None:
    raw_interrupts = result.get("__interrupt__")
    if raw_interrupts is None:
        return None
    if not isinstance(raw_interrupts, list | tuple) or not raw_interrupts:
        raise RuntimeError("graph returned an invalid interrupt envelope")
    first = raw_interrupts[0]
    payload = getattr(first, "value", None)
    interrupt_id = getattr(first, "id", None)
    if not isinstance(payload, dict):
        raise RuntimeError("graph interrupt payload must be a JSON object")
    if payload.get("type") != "operator_request":
        raise RuntimeError(f"unsupported graph interrupt type: {payload.get('type')}")
    if not isinstance(interrupt_id, str) or not interrupt_id:
        raise RuntimeError("graph interrupt is missing id")
    return OperatorInterrupt(interrupt_id=interrupt_id, payload=payload)


def _dict_or_empty(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value)
    return text if text else None


def _sum_optional(left: int | None, right: object) -> int | None:
    if not isinstance(right, int) or isinstance(right, bool):
        return left
    return (left or 0) + right


async def _gpu_metrics() -> list[GpuMetrics]:
    if shutil.which("nvidia-smi") is None:
        return [GpuMetrics(available=False)]
    process = await asyncio.create_subprocess_exec(
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,memory.used,memory.total",
        "--format=csv,noheader,nounits",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=3)
    except TimeoutError:
        process.kill()
        await process.wait()
        return [GpuMetrics(available=False, error="nvidia-smi timed out")]
    if process.returncode != 0:
        return [GpuMetrics(available=False, error=stderr.decode("utf-8", errors="replace")[-500:])]
    metrics: list[GpuMetrics] = []
    for line in stdout.decode("utf-8", errors="replace").splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) != 4:
            continue
        metrics.append(
            GpuMetrics(
                available=True,
                name=parts[0],
                utilization_percent=_optional_float(parts[1]),
                memory_used_mb=_optional_float(parts[2]),
                memory_total_mb=_optional_float(parts[3]),
            )
        )
    return metrics or [GpuMetrics(available=False, error="nvidia-smi returned no GPU rows")]


def _optional_float(value: str) -> float | None:
    try:
        return float(value)
    except ValueError:
        return None
