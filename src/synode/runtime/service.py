from __future__ import annotations

import asyncio
import os
import shutil
import time
from contextlib import asynccontextmanager, suppress
from typing import Any, AsyncIterator

from langgraph.checkpoint.memory import MemorySaver

from synode.config import Settings
from synode.models.provider import ModelProviderRegistry
from synode.observability import Observability
from synode.persistence.database import Database
from synode.persistence.repository import (
    Repository,
    to_agent_graph_response,
    to_agent_role_response,
    to_model_profile_response,
    to_run_response,
    to_secret_response,
    to_thread_message_response,
    to_thread_response,
)
from synode.registry import RoleRegistry, RoleSpec
from synode.runtime.graph import GraphDependencies, build_graph
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
    GpuMetrics,
    ModelProfileCreateRequest,
    ModelProfileResponse,
    ModelProfileUpdateRequest,
    ProcessMetrics,
    RoleName,
    RunEventResponse,
    RunMetricsResponse,
    RunMode,
    RunResponse,
    RunStatus,
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
)
from synode.security import SecretCipher
from synode.tools import ToolExecutor, ToolRegistry, build_tool_registry

ACTIVE_RUN_STATUSES = {
    RunStatus.CREATED.value,
    RunStatus.RUNNING.value,
    RunStatus.WAITING_APPROVAL.value,
}

TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED.value,
    RunStatus.FAILED.value,
    RunStatus.FAILED_VERIFICATION.value,
    RunStatus.CANCELLED.value,
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
        self.secret_cipher = SecretCipher(settings) if settings.secrets_key else None
        self.tool_executor = ToolExecutor(database, roles, tools, settings, self.observability)
        self._run_tasks: dict[str, asyncio.Task[None]] = {}
        self._stop_reasons: dict[str, str] = {}

    async def start_run(self, run_id: str) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                raise ValueError(f"run is terminal: {run_id}")
        task = self._run_tasks.get(run_id)
        if task is not None and not task.done():
            raise ValueError(f"run already has an active executor task: {run_id}")
        self._run_tasks[run_id] = asyncio.create_task(self._execute_run_background(run_id))

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

    async def run_task(
        self,
        task: str,
        workspace: str | None = None,
        model_provider: str | None = None,
        mode: RunMode = RunMode.GENERAL,
        default_model_profile_id: str | None = None,
        role_model_profile_ids: dict[str, str] | None = None,
        agent_graph_id: str | None = None,
    ) -> RunResponse:
        run = await self.create_run(
            task,
            workspace,
            model_provider,
            mode,
            default_model_profile_id,
            role_model_profile_ids,
            agent_graph_id,
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
                "default_model_profile_id": default_model_profile_id,
                "role_model_profile_ids": role_model_profile_ids,
                "agent_graph_id": agent_graph_id,
                "agent_graph_snapshot": agent_graph_snapshot,
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
        except asyncio.CancelledError:
            await self._mark_run_cancelled(run_id, self._stop_reasons.pop(run_id, "Run stopped by user."))
            raise
        except Exception as exc:
            async with self.database.session() as session:
                repo = Repository(session)
                run = await repo.get_run(run_id)
                if run is not None and run.status == RunStatus.CANCELLED.value:
                    return
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
        await self._mark_run_cancelled(approval.run_id, f"Approval rejected for {approval.tool_name}.")

    async def resume_run(self, run_id: str) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                raise ValueError(f"run is terminal: {run_id}")
        await self.execute_run(run_id)

    async def stop_run(self, run_id: str, reason: str | None = None) -> RunResponse:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                return to_run_response(run)
        task = self._run_tasks.get(run_id)
        if task is not None and not task.done():
            self._stop_reasons[run_id] = reason or "Run stopped by user."
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        else:
            await self._mark_run_cancelled(run_id, reason or "Run stopped by user.")
        return await self.get_run(run_id)

    async def _mark_run_cancelled(self, run_id: str, reason: str) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            if run.status in TERMINAL_RUN_STATUSES:
                return
            await repo.reject_pending_approvals(run_id, reason)
            await repo.set_run_status(run_id, RunStatus.CANCELLED, final_answer=reason, error=reason)
            await repo.add_event(run_id, EventType.RUN_CANCELLED.value, None, {"reason": reason})
            await repo.add_thread_message(
                run.thread_id,
                author_type=ThreadMessageAuthorType.SYSTEM,
                author_name="runtime",
                message_type=ThreadMessageType.RUN_SUMMARY,
                content=f"Run cancelled: {reason}",
                run_id=run_id,
                metadata={"status": RunStatus.CANCELLED.value},
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
                role_ids=payload.role_ids,
                edges=[edge.model_dump(mode="json") for edge in payload.edges],
                default_model_profile_id=payload.default_model_profile_id,
                role_model_profile_ids=payload.role_model_profile_ids,
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
            if "edges" in values:
                values["edges"] = [edge.model_dump(mode="json") for edge in payload.edges or []]
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
        for role_id in graph.role_ids or []:
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
        edges: list[dict[str, str]] = []
        for edge in graph.edges or []:
            source = roles_by_id.get(edge.get("from_role"))
            target = roles_by_id.get(edge.get("to_role"))
            if source is None or target is None:
                raise ValueError("agent graph edges must reference enabled graph roles")
            edges.append({"from_role": source.name, "to_role": target.name})
        return (
            {
                "id": graph.id,
                "name": graph.name,
                "roles": roles,
                "edges": edges,
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

    async def _invoke_graph(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
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
        )
        async with self._checkpointer() as checkpointer:
            graph = build_graph(deps, checkpointer=checkpointer)
            result = await graph.ainvoke(state, config={"configurable": {"thread_id": run_id}})
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
            yield MemorySaver()

    async def close(self) -> None:
        for task in list(self._run_tasks.values()):
            if not task.done():
                task.cancel()
        for task in list(self._run_tasks.values()):
            with suppress(asyncio.CancelledError):
                await task
        self._run_tasks.clear()
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
    database = Database(settings)
    roles = RoleRegistry.load_builtin()
    models = ModelProviderRegistry(settings)
    observability = Observability(settings)
    tools = await build_tool_registry(settings, include_mcp=include_mcp)
    service = OrchestrationService(settings, database, roles, models, tools, observability)
    await service.ensure_default_configuration()
    return service


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
