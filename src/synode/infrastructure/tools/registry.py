from __future__ import annotations

from synode.infrastructure.config import Settings
from synode.infrastructure.tools.base import BaseTool, ToolRegistry
from synode.infrastructure.tools.coding import (
    GitDiffTool,
    GitStatusTool,
    PatchApplyTool,
    VerifyTool,
)
from synode.infrastructure.tools.data import DataProfileTool, PythonSandboxTool
from synode.infrastructure.tools.database import DatabaseReadonlyTool
from synode.infrastructure.tools.filesystem import (
    FileListTool,
    FileReadTool,
    FileSearchTool,
    FileWriteTool,
)
from synode.infrastructure.tools.shell import ShellTool
from synode.infrastructure.tools.web import WebFetchTool, WebSearchTool


async def build_tool_registry(settings: Settings, include_mcp: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    native_tools: list[BaseTool] = [
        FileReadTool(),
        FileListTool(),
        FileSearchTool(),
        FileWriteTool(),
        GitStatusTool(),
        GitDiffTool(),
        PatchApplyTool(),
        VerifyTool(),
        ShellTool(),
        DataProfileTool(),
        PythonSandboxTool(),
        WebSearchTool(),
        WebFetchTool(),
        DatabaseReadonlyTool(),
    ]
    for tool in native_tools:
        registry.register(tool)
    return registry
