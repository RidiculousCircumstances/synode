from __future__ import annotations

from typing import Any

from synode.application.orchestration import OrchestrationService
from synode.application.ports import ToolRegistryPort
from synode.domain.roles import RoleRegistry
from synode.infrastructure.config import Settings
from synode.infrastructure.models.provider import ModelProviderRegistry
from synode.infrastructure.observability import Observability
from synode.infrastructure.persistence.database import Database
from synode.infrastructure.persistence.repository import Repository
from synode.infrastructure.runtime.execution import build_execution_backend_registry
from synode.infrastructure.runtime.queue import build_run_queue_transport
from synode.infrastructure.security import SecretCipher
from synode.infrastructure.tools import ToolExecutor, build_tool_registry
from synode.infrastructure.tools.mcp import (
    MCPServerRuntimeConfig,
    discover_mcp_tools,
    register_mcp_tools,
)
from synode.infrastructure.tools.sandbox import SandboxRunner


class InfrastructureMCPToolManager:
    async def discover(self, name: str, config: dict[str, Any]) -> list[str]:
        return await discover_mcp_tools(name, config)

    def register(self, tools: ToolRegistryPort, runtime_configs: list[dict[str, Any]]) -> None:
        configs = [
            MCPServerRuntimeConfig(
                name=str(config["name"]),
                config=dict(config.get("config") or {}),
                tools=list(config.get("tools") or []),
            )
            for config in runtime_configs
        ]
        register_mcp_tools(tools, configs)


async def create_service(settings: Settings, include_mcp: bool = True) -> OrchestrationService:
    settings.validate_startup()
    database = Database(settings)
    roles = RoleRegistry.load_builtin()
    models = ModelProviderRegistry(settings)
    observability = Observability(settings)
    tools = await build_tool_registry(settings, include_mcp=include_mcp)
    run_queue = build_run_queue_transport(settings)
    execution_backends = build_execution_backend_registry(settings, database)
    secret_cipher = SecretCipher(settings) if settings.secrets_key else None

    def tool_executor_factory(role_registry: RoleRegistry) -> ToolExecutor:
        return ToolExecutor(database, role_registry, tools, settings, observability)

    service = OrchestrationService(
        settings=settings,
        database=database,
        roles=roles,
        models=models,
        tools=tools,
        observability=observability,
        run_queue=run_queue,
        execution_backends=execution_backends,
        secret_cipher=secret_cipher,
        tool_executor=tool_executor_factory(roles),
        tool_executor_factory=tool_executor_factory,
        repository_factory=Repository,
        sandbox_status_factory=lambda: SandboxRunner(settings).status(),
        mcp_tool_manager=InfrastructureMCPToolManager(),
    )
    try:
        await run_queue.open()
        await service.ensure_default_configuration()
        if include_mcp:
            await service.refresh_mcp_tools()
        return service
    except Exception:
        await service.close()
        raise
