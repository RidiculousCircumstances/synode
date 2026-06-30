from __future__ import annotations

from typing import Any

from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext

SAFE_COMMANDS = {"rg", "ls", "pwd", "git", "pytest", "python", "python3", "uv"}
SAFE_GIT_SUBCOMMANDS = {"status", "diff", "show", "log"}
SAFE_UV_SUBCOMMANDS = {"run"}
SAFE_PYTHON_MODULES = {"pytest"}


def is_safe_command(argv: list[str]) -> bool:
    if not argv:
        return False
    command = str(argv[0])
    if command not in SAFE_COMMANDS:
        return False
    if command == "git" and len(argv) > 1 and str(argv[1]) not in SAFE_GIT_SUBCOMMANDS:
        return False
    if command == "uv" and len(argv) > 1 and str(argv[1]) not in SAFE_UV_SUBCOMMANDS:
        return False
    if command in {"python", "python3"} and len(argv) > 2 and argv[1] == "-m" and argv[2] not in SAFE_PYTHON_MODULES:
        return False
    return True


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
