from __future__ import annotations

import asyncio
import os
import shutil
import time
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from langgraph.checkpoint.memory import MemorySaver

from synode.config import Settings
from synode.models.provider import ModelProviderRegistry
from synode.observability import Observability
from synode.persistence.database import Database
from synode.persistence.repository import (
    Repository,
    to_run_response,
    to_thread_message_response,
    to_thread_response,
)
from synode.registry import RoleRegistry
from synode.runtime.graph import GraphDependencies, build_graph
from synode.schemas import (
    ApprovalResponse,
    ApprovalStatus,
    ArtifactResponse,
    EventType,
    GpuMetrics,
    ProcessMetrics,
    RunEventResponse,
    RunMetricsResponse,
    RunMode,
    RunResponse,
    RunStatus,
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
)
from synode.tools import ToolExecutor, ToolRegistry, build_tool_registry

ACTIVE_RUN_STATUSES = {
    RunStatus.CREATED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_APPROVAL.value,
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
    ):
        self.settings = settings
        self.database = database
        self.roles = roles
        self.models = models
        self.tools = tools
        self.observability = observability or Observability(settings)
        self.tool_executor = ToolExecutor(database, roles, tools, settings, self.observability)

    async def create_run(
        self,
        task: str,
        workspace: str | None = None,
        model_provider: str | None = None,
        mode: RunMode = RunMode.GENERAL,
    ) -> RunResponse:
        provider = model_provider or self.settings.model_provider
        trace_id = self.observability.create_trace_id()
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.create_run(
                task=task,
                workspace=workspace,
                model_provider=provider,
                mode=mode,
                observability_trace_id=trace_id,
            )
            return to_run_response(run)

    async def create_thread(
        self,
        message: str,
        title: str | None = None,
        workspace: str | None = None,
        model_provider: str | None = None,
        mode: RunMode = RunMode.GENERAL,
    ) -> ThreadDetailResponse:
        provider = model_provider or self.settings.model_provider
        trace_id = self.observability.create_trace_id()
        async with self.database.session() as session:
            repo = Repository(session)
            thread = await repo.create_thread(title or message)
            await repo.create_run(
                task=message,
                workspace=workspace,
                model_provider=provider,
                mode=mode,
                observability_trace_id=trace_id,
                thread_id=thread.id,
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

    async def get_thread(self, thread_id: str) -> ThreadDetailResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            return await self._thread_detail(repo, thread_id)

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
    ) -> RunResponse:
        provider = model_provider or self.settings.model_provider
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
            run = await repo.create_run(
                task=message,
                workspace=workspace,
                model_provider=provider,
                mode=mode,
                observability_trace_id=trace_id,
                thread_id=thread_id,
            )
            return to_run_response(run)

    async def list_thread_messages(self, thread_id: str) -> list[ThreadMessageResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            if await repo.get_thread(thread_id) is None:
                raise LookupError(f"thread not found: {thread_id}")
            return [to_thread_message_response(message) for message in await repo.list_thread_messages(thread_id)]

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

    async def list_events(self, run_id: str, after_id: int = 0) -> list[dict[str, Any]]:
        async with self.database.session() as session:
            repo = Repository(session)
            events = await repo.list_events(run_id, after_id=after_id)
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

    async def list_event_responses(self, run_id: str, after_id: int = 0) -> list[RunEventResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            events = await repo.list_events(run_id, after_id=after_id)
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

    async def list_artifacts(self, run_id: str) -> list[ArtifactResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            artifacts = await repo.list_artifacts(run_id)
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

    async def list_tool_audit(self, run_id: str) -> list[ToolAuditResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            audit = await repo.list_tool_audit(run_id)
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
        self, run_id: str | None = None, status: ApprovalStatus | None = None
    ) -> list[ApprovalResponse]:
        async with self.database.session() as session:
            repo = Repository(session)
            approvals = await repo.list_approvals(run_id=run_id, status=status)
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

    async def run_task(
        self,
        task: str,
        workspace: str | None = None,
        model_provider: str | None = None,
        mode: RunMode = RunMode.GENERAL,
    ) -> RunResponse:
        run = await self.create_run(task, workspace, model_provider, mode)
        await self.execute_run(run.id)
        return await self.get_run(run.id)

    async def execute_run(self, run_id: str) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            await repo.set_run_status(run_id, RunStatus.RUNNING)
            await repo.add_event(run_id, EventType.RUN_STARTED.value, None, {})
            task = run.task
            workspace = run.workspace
            model_provider = run.model_provider
            mode = run.mode
            trace_id = run.observability_trace_id
            thread_id = run.thread_id
            await repo.add_thread_message(
                thread_id,
                author_type=ThreadMessageAuthorType.SYSTEM,
                author_name="runtime",
                message_type=ThreadMessageType.RUN_SUMMARY,
                content="Run started.",
                run_id=run_id,
                metadata={"status": RunStatus.RUNNING.value},
            )

        try:
            state: dict[str, Any] = {
                "run_id": run_id,
                "task": task,
                "workspace": workspace,
                "model_provider": model_provider,
                "mode": mode,
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
                final_state = await self._invoke_graph(run_id, state)
                self.observability.update_current_span(output={"status": "finished"})
            review = final_state.get("review", {})
            final_answer = final_state.get("final_answer", "")
            async with self.database.session() as session:
                repo = Repository(session)
                blockers = list(review.get("blockers", []))
                if any("Approval required" in blocker for blocker in blockers):
                    await repo.set_run_status(run_id, RunStatus.WAITING_APPROVAL, final_answer=final_answer)
                    await repo.add_thread_message(
                        thread_id,
                        author_type=ThreadMessageAuthorType.SYSTEM,
                        author_name="runtime",
                        message_type=ThreadMessageType.RUN_SUMMARY,
                        content="Run is waiting for approval.",
                        run_id=run_id,
                        metadata={"status": RunStatus.WAITING_APPROVAL.value},
                    )
                elif mode == RunMode.CODING.value and not review.get("can_proceed", False):
                    await repo.set_run_status(run_id, RunStatus.FAILED_VERIFICATION, final_answer=final_answer)
                    await repo.add_thread_message(
                        thread_id,
                        author_type=ThreadMessageAuthorType.AGENT,
                        author_name="reviewer",
                        message_type=ThreadMessageType.FINAL,
                        content=final_answer or "Run failed verification.",
                        run_id=run_id,
                        metadata={"status": RunStatus.FAILED_VERIFICATION.value},
                    )
                else:
                    await repo.set_run_status(run_id, RunStatus.COMPLETED, final_answer=final_answer)
                    await repo.add_event(run_id, EventType.RUN_COMPLETED.value, None, {})
                    await repo.add_thread_message(
                        thread_id,
                        author_type=ThreadMessageAuthorType.AGENT,
                        author_name="synode",
                        message_type=ThreadMessageType.FINAL,
                        content=final_answer or "Run completed.",
                        run_id=run_id,
                        metadata={"status": RunStatus.COMPLETED.value},
                    )
        except Exception as exc:
            async with self.database.session() as session:
                repo = Repository(session)
                run = await repo.get_run(run_id)
                await repo.set_run_status(run_id, RunStatus.FAILED, error=str(exc))
                await repo.add_event(run_id, EventType.RUN_FAILED.value, None, {"error": str(exc)})
                if run is not None:
                    await repo.add_thread_message(
                        run.thread_id,
                        author_type=ThreadMessageAuthorType.SYSTEM,
                        author_name="runtime",
                        message_type=ThreadMessageType.RUN_SUMMARY,
                        content=f"Run failed: {exc}",
                        run_id=run_id,
                        metadata={"status": RunStatus.FAILED.value},
                    )
            raise

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

    async def reject(self, approval_id: str, reason: str | None = None) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            approval = await repo.decide_approval(approval_id, ApprovalStatus.REJECTED, reason)
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

    async def resume_run(self, run_id: str) -> None:
        await self.execute_run(run_id)

    async def model_health(self) -> list[dict[str, object]]:
        return [health.model_dump(mode="json") for health in await self.models.health()]

    async def run_metrics(self, run_id: str) -> RunMetricsResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            events = await repo.list_events(run_id)
            audit = await repo.list_tool_audit(run_id)
            approvals = await repo.list_approvals(run_id=run_id)

        model_events = [event for event in events if event.event_type == EventType.MODEL_INVOKED.value]
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
            event_count=len(events),
            model_call_count=len(model_events),
            tool_call_count=len(audit),
            approval_count=len(approvals),
            pending_approval_count=len(
                [approval for approval in approvals if approval.status == ApprovalStatus.PENDING.value]
            ),
            failed_tool_call_count=len([record for record in audit if record.status in {"denied", "error"}]),
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

    async def _invoke_graph(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        deps = GraphDependencies(
            database=self.database,
            roles=self.roles,
            models=self.models,
            tool_executor=self.tool_executor,
            observability=self.observability,
        )
        async with self._checkpointer() as checkpointer:
            graph = build_graph(deps, checkpointer=checkpointer)
            result = await graph.ainvoke(state, config={"configurable": {"thread_id": run_id}})
            return dict(result)

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
            yield MemorySaver()

    async def close(self) -> None:
        await self.database.close()
        self.observability.shutdown()

    async def _thread_detail(self, repo: Repository, thread_id: str) -> ThreadDetailResponse:
        thread = await repo.get_thread(thread_id)
        if thread is None:
            raise LookupError(f"thread not found: {thread_id}")
        runs = await repo.list_thread_runs(thread_id)
        messages = await repo.list_thread_messages(thread_id)
        latest_run = runs[0] if runs else None
        latest_message = messages[-1] if messages else None
        return ThreadDetailResponse(
            thread=to_thread_response(thread, latest_run=latest_run, latest_message=latest_message),
            runs=[to_run_response(run) for run in runs],
            messages=[to_thread_message_response(message) for message in messages],
        )


async def create_service(settings: Settings, include_mcp: bool = True) -> OrchestrationService:
    database = Database(settings)
    roles = RoleRegistry.load_builtin()
    models = ModelProviderRegistry(settings)
    observability = Observability(settings)
    tools = await build_tool_registry(settings, include_mcp=include_mcp)
    return OrchestrationService(settings, database, roles, models, tools, observability)


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
