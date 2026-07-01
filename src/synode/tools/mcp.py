from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext

WRITE_HINTS = ("write", "create", "update", "delete", "remove", "mutate", "merge", "push", "deploy")


@dataclass(frozen=True)
class MCPServerRuntimeConfig:
    name: str
    config: dict[str, Any]
    tools: list[str]


class MCPTool:
    def __init__(self, server_name: str, tool_name: str, server_config: dict[str, Any]):
        self.server_name = server_name
        self.remote_tool_name = tool_name
        self.name = f"mcp.{server_name}.{tool_name}"
        self.server_config = server_config

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        name = self.remote_tool_name.lower()
        if any(hint in name for hint in WRITE_HINTS):
            return ToolRisk.WRITE
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as exc:
            return ToolResult(tool_name=self.name, ok=False, error=f"MCP adapter missing: {exc}")

        client = MultiServerMCPClient(cast(Any, {self.server_name: self.server_config}))
        tools = await client.get_tools()
        remote = next((tool for tool in tools if getattr(tool, "name", None) == self.remote_tool_name), None)
        if remote is None:
            return ToolResult(tool_name=self.name, ok=False, error=f"MCP tool not found: {self.remote_tool_name}")
        result = await remote.ainvoke(arguments)
        return ToolResult(tool_name=self.name, ok=True, output={"result": result})


async def discover_mcp_tools(server_name: str, server_config: dict[str, Any]) -> list[str]:
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:
        raise RuntimeError("langchain-mcp-adapters is required for MCP discovery") from exc
    client = MultiServerMCPClient(cast(Any, {server_name: server_config}))
    tools = await client.get_tools()
    return sorted(str(getattr(tool, "name", "")) for tool in tools if getattr(tool, "name", ""))


def register_mcp_tools(registry: Any, servers: list[MCPServerRuntimeConfig]) -> None:
    for server in servers:
        for remote_name in server.tools:
            registry.register(MCPTool(server.name, remote_name, server.config))
