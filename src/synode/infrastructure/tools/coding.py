from __future__ import annotations

from typing import Any

from synode.domain.models import ToolResult, ToolRisk
from synode.domain.runtime.commands import is_safe_command
from synode.infrastructure.tools.base import ToolContext
from synode.infrastructure.tools.mutations import run_sandboxed_patch_apply


class GitStatusTool:
    name = "native.git_status"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        return await _run_command(context, ["git", "status", "--short"], self.name)


class GitDiffTool:
    name = "native.git_diff"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        argv = ["git", "diff"]
        if arguments.get("cached"):
            argv.append("--cached")
        return await _run_command(context, argv, self.name)


class PatchApplyTool:
    name = "native.patch_apply"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.WRITE

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        context.sandbox.ensure_available()
        return await run_sandboxed_patch_apply(context, patches=list(arguments.get("patches", [])))


class VerifyTool:
    name = "native.verify"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        commands = list(arguments.get("commands", []))
        if not commands:
            return ToolResult(tool_name=self.name, ok=False, error="commands are required")
        results: list[dict[str, Any]] = []
        for command in commands:
            argv = [str(part) for part in command]
            if not is_safe_command(argv):
                return ToolResult(tool_name=self.name, ok=False, error=f"unsafe verification command: {argv}")
            result = await _run_command(context, argv, self.name)
            results.append(result.output)
            if not result.ok:
                return ToolResult(tool_name=self.name, ok=False, output={"commands": results})
        return ToolResult(tool_name=self.name, ok=True, output={"commands": results})


async def _run_command(context: ToolContext, argv: list[str], tool_name: str) -> ToolResult:
    cwd = context.workspace_policy.resolve_workspace(context.workspace)
    result = await context.sandbox.run_command(
        argv,
        cwd=cwd,
        timeout=context.settings.shell_timeout_seconds,
    )
    return ToolResult(
        tool_name=tool_name,
        ok=result.ok,
        error=result.error,
        output={
            "argv": argv,
            "returncode": result.returncode,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )
