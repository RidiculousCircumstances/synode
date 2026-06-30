from __future__ import annotations

import asyncio
from typing import Any

from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext

SAFE_COMMANDS = {"rg", "ls", "pwd", "git", "pytest", "python", "python3", "uv"}
SAFE_GIT_SUBCOMMANDS = {"status", "diff", "show", "log"}
SAFE_UV_SUBCOMMANDS = {"run"}
SAFE_PYTHON_MODULES = {"pytest"}


class ShellTool:
    name = "native.shell"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        argv = list(arguments.get("argv", []))
        if not argv:
            return ToolRisk.READ
        command = str(argv[0])
        if command not in SAFE_COMMANDS:
            return ToolRisk.WRITE
        if command == "git" and len(argv) > 1 and str(argv[1]) not in SAFE_GIT_SUBCOMMANDS:
            return ToolRisk.WRITE
        if command == "uv" and len(argv) > 1 and str(argv[1]) not in SAFE_UV_SUBCOMMANDS:
            return ToolRisk.WRITE
        if command in {"python", "python3"} and len(argv) > 2 and argv[1] == "-m" and argv[2] not in SAFE_PYTHON_MODULES:
            return ToolRisk.WRITE
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        argv = [str(part) for part in arguments.get("argv", [])]
        if not argv:
            return ToolResult(tool_name=self.name, ok=False, error="argv is required")
        cwd = context.workspace_policy.resolve_workspace(context.workspace)
        timeout = float(arguments.get("timeout", context.settings.shell_timeout_seconds))
        process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=str(cwd),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), timeout=timeout)
        except TimeoutError:
            process.kill()
            await process.wait()
            return ToolResult(tool_name=self.name, ok=False, error=f"command timed out after {timeout}s")
        output = {
            "argv": argv,
            "returncode": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[-12000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-12000:],
        }
        return ToolResult(tool_name=self.name, ok=process.returncode == 0, output=output)

