from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from langgraph.checkpoint.memory import MemorySaver

from synode.config import Settings
from synode.models.provider import ModelProviderRegistry
from synode.persistence.database import Database
from synode.persistence.repository import Repository, to_run_response
from synode.registry import RoleRegistry
from synode.runtime.graph import GraphDependencies, build_graph
from synode.schemas import ApprovalStatus, EventType, RunResponse, RunStatus
from synode.tools import ToolExecutor, ToolRegistry, build_tool_registry


class OrchestrationService:
    def __init__(
        self,
        settings: Settings,
        database: Database,
        roles: RoleRegistry,
        models: ModelProviderRegistry,
        tools: ToolRegistry,
    ):
        self.settings = settings
        self.database = database
        self.roles = roles
        self.models = models
        self.tools = tools
        self.tool_executor = ToolExecutor(database, roles, tools, settings)

    async def create_run(
        self, task: str, workspace: str | None = None, model_provider: str | None = None
    ) -> RunResponse:
        provider = model_provider or self.settings.model_provider
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.create_run(task=task, workspace=workspace, model_provider=provider)
            return to_run_response(run)

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

    async def run_task(
        self, task: str, workspace: str | None = None, model_provider: str | None = None
    ) -> RunResponse:
        run = await self.create_run(task, workspace, model_provider)
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

        try:
            state: dict[str, Any] = {
                "run_id": run_id,
                "task": task,
                "workspace": workspace,
                "model_provider": model_provider,
                "worker_outputs": [],
            }
            final_state = await self._invoke_graph(run_id, state)
            review = final_state.get("review", {})
            final_answer = final_state.get("final_answer", "")
            async with self.database.session() as session:
                repo = Repository(session)
                if review.get("blockers"):
                    await repo.set_run_status(run_id, RunStatus.WAITING_APPROVAL, final_answer=final_answer)
                else:
                    await repo.set_run_status(run_id, RunStatus.COMPLETED, final_answer=final_answer)
                    await repo.add_event(run_id, EventType.RUN_COMPLETED.value, None, {})
        except Exception as exc:
            async with self.database.session() as session:
                repo = Repository(session)
                await repo.set_run_status(run_id, RunStatus.FAILED, error=str(exc))
                await repo.add_event(run_id, EventType.RUN_FAILED.value, None, {"error": str(exc)})
            raise

    async def approve(self, approval_id: str, reason: str | None = None) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            await repo.decide_approval(approval_id, ApprovalStatus.APPROVED, reason)

    async def reject(self, approval_id: str, reason: str | None = None) -> None:
        async with self.database.session() as session:
            repo = Repository(session)
            await repo.decide_approval(approval_id, ApprovalStatus.REJECTED, reason)

    async def resume_run(self, run_id: str) -> None:
        await self.execute_run(run_id)

    async def _invoke_graph(self, run_id: str, state: dict[str, Any]) -> dict[str, Any]:
        deps = GraphDependencies(
            database=self.database,
            roles=self.roles,
            models=self.models,
            tool_executor=self.tool_executor,
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


async def create_service(settings: Settings, include_mcp: bool = True) -> OrchestrationService:
    database = Database(settings)
    roles = RoleRegistry.load_builtin()
    models = ModelProviderRegistry()
    tools = await build_tool_registry(settings, include_mcp=include_mcp)
    return OrchestrationService(settings, database, roles, models, tools)
