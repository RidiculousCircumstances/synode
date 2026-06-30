from __future__ import annotations

import pathlib

import pytest

from synode.config import Settings
from synode.models.provider import ModelProviderRegistry
from synode.persistence.database import Database
from synode.registry import RoleRegistry
from synode.runtime.service import OrchestrationService
from synode.tools import ToolExecutor, build_tool_registry


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
    return OrchestrationService(settings, database, roles, models, tools)


@pytest.fixture()
async def tool_executor(settings: Settings, database: Database) -> ToolExecutor:
    roles = RoleRegistry.load_builtin()
    tools = await build_tool_registry(settings, include_mcp=False)
    return ToolExecutor(database, roles, tools, settings)
