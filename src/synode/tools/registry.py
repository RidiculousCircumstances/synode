from __future__ import annotations

from synode.config import Settings
from synode.tools.base import BaseTool, ToolRegistry
from synode.tools.data import DataProfileTool, PythonSandboxTool
from synode.tools.database import DatabaseReadonlyTool
from synode.tools.filesystem import FileReadTool, FileSearchTool, FileWriteTool
from synode.tools.mcp import register_mcp_tools
from synode.tools.shell import ShellTool
from synode.tools.web import WebFetchTool, WebSearchTool


async def build_tool_registry(settings: Settings, include_mcp: bool = True) -> ToolRegistry:
    registry = ToolRegistry()
    native_tools: list[BaseTool] = [
        FileReadTool(),
        FileSearchTool(),
        FileWriteTool(),
        ShellTool(),
        DataProfileTool(),
        PythonSandboxTool(),
        WebSearchTool(),
        WebFetchTool(),
        DatabaseReadonlyTool(),
    ]
    for tool in native_tools:
        registry.register(tool)
    if include_mcp:
        await register_mcp_tools(registry, settings)
    return registry
