from __future__ import annotations

from typing import Any

from synode.domain.models import ToolResult, ToolRisk
from synode.domain.runtime.commands import is_safe_command
from synode.infrastructure.tools.base import ToolContext


class ShellTool:
    name = "native.shell"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        argv = list(arguments.get("argv", []))
        return ToolRisk.READ if is_safe_command([str(part) for part in argv]) else ToolRisk.WRITE

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        argv = [str(part) for part in arguments.get("argv", [])]
        if not argv:
            return ToolResult(tool_name=self.name, ok=False, error="argv is required")
        cwd = context.workspace_policy.resolve_workspace(context.workspace)
        timeout = float(arguments.get("timeout", context.settings.shell_timeout_seconds))
        result = await context.sandbox.run_command(argv, cwd=cwd, timeout=timeout)
        output = {
            "argv": argv,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        }
        return ToolResult(tool_name=self.name, ok=result.ok, output=output, error=result.error)
