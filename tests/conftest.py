from __future__ import annotations

import pathlib

import pytest

from synode.application.orchestration import OrchestrationService
from synode.domain.roles import RoleRegistry
from synode.infrastructure.composition import InfrastructureMCPToolManager
from synode.infrastructure.config import Settings
from synode.infrastructure.models.provider import ModelProviderRegistry
from synode.infrastructure.observability import Observability
from synode.infrastructure.persistence.database import Database
from synode.infrastructure.persistence.repository import Repository
from synode.infrastructure.runtime.execution import ExecutionBackendRegistry
from synode.infrastructure.runtime.queue import InMemoryRunQueueTransport
from synode.infrastructure.security import SecretCipher
from synode.infrastructure.tools import ToolExecutor, build_tool_registry
from synode.infrastructure.tools.sandbox import SandboxRunner


@pytest.fixture()
def settings(tmp_path: pathlib.Path) -> Settings:
    return Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        enable_postgres_checkpointer=False,
        workspace_allowlist=str(tmp_path),
        mcp_config_path=tmp_path / ".mcp.json",
    )


@pytest.fixture()
async def database(settings: Settings) -> Database:
    db = Database(settings)
    await db.create_schema()
    try:
        yield db
    finally:
        await db.close()


@pytest.fixture()
async def service(settings: Settings, database: Database) -> OrchestrationService:
    roles = RoleRegistry.load_builtin()
    models = ModelProviderRegistry()
    tools = await build_tool_registry(settings, include_mcp=False)
    observability = Observability(settings)
    tool_executor_factory = lambda role_registry: ToolExecutor(  # noqa: E731
        database,
        role_registry,
        tools,
        settings,
        observability,
    )
    return OrchestrationService(
        settings=settings,
        database=database,
        roles=roles,
        models=models,
        tools=tools,
        observability=observability,
        run_queue=InMemoryRunQueueTransport(),
        execution_backends=ExecutionBackendRegistry(settings, database),
        secret_cipher=SecretCipher(settings) if settings.secrets_key else None,
        tool_executor=tool_executor_factory(roles),
        tool_executor_factory=tool_executor_factory,
        repository_factory=Repository,
        sandbox_status_factory=lambda: SandboxRunner(settings).status(),
        mcp_tool_manager=InfrastructureMCPToolManager(),
    )


@pytest.fixture()
async def tool_executor(settings: Settings, database: Database) -> ToolExecutor:
    roles = RoleRegistry.load_builtin()
    tools = await build_tool_registry(settings, include_mcp=False)
    return ToolExecutor(database, roles, tools, settings)
