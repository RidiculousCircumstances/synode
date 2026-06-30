from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from synode.config import Settings
from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext

WRITE_HINTS = ("write", "create", "update", "delete", "remove", "mutate", "merge", "push", "deploy")


class MCPBridge:
    def __init__(self, settings: Settings):
        self.settings = settings

    def load_config(self) -> dict[str, Any]:
        path = Path(self.settings.mcp_config_path)
        if not path.is_absolute():
            path = Path.cwd() / path
        if not path.exists():
            return {"mcpServers": {}}
        return json.loads(path.read_text(encoding="utf-8"))

    async def discover(self) -> list[str]:
        config = self.load_config()
        if not config.get("mcpServers"):
            return []
        try:
            from langchain_mcp_adapters.client import MultiServerMCPClient
        except ImportError as exc:
            raise RuntimeError("langchain-mcp-adapters is required for MCP discovery") from exc
        client = MultiServerMCPClient(config["mcpServers"])
        tools = await client.get_tools()
        return sorted(f"mcp.{getattr(tool, 'name', 'unknown')}" for tool in tools)


class MCPTool:
    def __init__(self, server_name: str, tool_name: str, settings: Settings):
        self.server_name = server_name
        self.remote_tool_name = tool_name
        self.name = f"mcp.{server_name}.{tool_name}"
        self.settings = settings

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

        config = MCPBridge(context.settings).load_config()
        server_config = config.get("mcpServers", {}).get(self.server_name)
        if not server_config:
            return ToolResult(tool_name=self.name, ok=False, error=f"MCP server not configured: {self.server_name}")
        client = MultiServerMCPClient({self.server_name: server_config})
        tools = await client.get_tools()
        remote = next((tool for tool in tools if getattr(tool, "name", None) == self.remote_tool_name), None)
        if remote is None:
            return ToolResult(tool_name=self.name, ok=False, error=f"MCP tool not found: {self.remote_tool_name}")
        result = await remote.ainvoke(arguments)
        return ToolResult(tool_name=self.name, ok=True, output={"result": result})


async def register_mcp_tools(registry: Any, settings: Settings) -> None:
    config = MCPBridge(settings).load_config()
    servers = config.get("mcpServers", {})
    if not servers:
        return
    try:
        from langchain_mcp_adapters.client import MultiServerMCPClient
    except ImportError as exc:
        raise RuntimeError("langchain-mcp-adapters is required when MCP servers are configured") from exc
    for server_name, server_config in servers.items():
        client = MultiServerMCPClient({server_name: server_config})
        tools = await client.get_tools()
        for tool in tools:
            remote_name = str(getattr(tool, "name", ""))
            registry.register(MCPTool(server_name, remote_name, settings))
