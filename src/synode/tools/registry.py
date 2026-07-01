from __future__ import annotations

from synode.config import Settings
from synode.tools.base import BaseTool, ToolRegistry
from synode.tools.coding import GitDiffTool, GitStatusTool, PatchApplyTool, VerifyTool
from synode.tools.data import DataProfileTool, PythonSandboxTool
from synode.tools.database import DatabaseReadonlyTool
from synode.tools.filesystem import FileReadTool, FileSearchTool, FileWriteTool
from synode.tools.shell import ShellTool
from synode.tools.web import WebFetchTool, WebSearchTool


async def build_tool_registry(settings: Settings, include_mcp: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    native_tools: list[BaseTool] = [
        FileReadTool(),
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
