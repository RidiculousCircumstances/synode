from __future__ import annotations

from synode.registry import RoleRegistry
from synode.runtime.routing import select_worker_roles


def test_builtin_roles_load() -> None:
    roles = RoleRegistry.load_builtin()
    names = {role.name for role in roles.list()}
    assert {"supervisor", "coder", "data_analyst", "web_researcher", "db_agent", "reviewer"} <= names
    assert roles.get("coder").allows_tool("native.fs_read")
    assert roles.get("coder").allows_tool("native.fs_list")
    assert roles.get("coder").allows_tool("mcp.github.get_issue")


def test_routing_selects_multiple_roles() -> None:
    roles = select_worker_roles("Analyze csv data, search web docs, and inspect postgres sql")
    assert roles == ["data_analyst", "web_researcher", "db_agent"]
