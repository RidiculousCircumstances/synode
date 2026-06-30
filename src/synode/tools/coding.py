from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext
from synode.tools.shell import is_safe_command


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
        patches = list(arguments.get("patches", []))
        if not patches:
            return ToolResult(tool_name=self.name, ok=False, risk=ToolRisk.WRITE, error="patches are required")
        changed: list[dict[str, Any]] = []
        for patch in patches:
            path = context.workspace_policy.resolve_path(context.workspace, str(patch["path"]))
            if not path.exists() or not path.is_file():
                return ToolResult(
                    tool_name=self.name,
                    ok=False,
                    risk=ToolRisk.WRITE,
                    error=f"patch target is not an existing file: {path}",
                )
            old_content = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(old_content.encode("utf-8")).hexdigest()
            expected_sha256 = str(patch["expected_sha256"])
            if digest != expected_sha256:
                return ToolResult(
                    tool_name=self.name,
                    ok=False,
                    risk=ToolRisk.WRITE,
                    error=f"checksum mismatch for {path}: expected {expected_sha256}, got {digest}",
                )
            old_text = str(patch["old_text"])
            new_text = str(patch["new_text"])
            occurrences = old_content.count(old_text)
            if occurrences != 1:
                return ToolResult(
                    tool_name=self.name,
                    ok=False,
                    risk=ToolRisk.WRITE,
                    error=f"old_text must occur exactly once in {path}; occurrences={occurrences}",
                )
            path.write_text(old_content.replace(old_text, new_text, 1), encoding="utf-8")
            changed.append({"path": str(path), "old_sha256": digest})
        return ToolResult(
            tool_name=self.name,
            ok=True,
            risk=ToolRisk.WRITE,
            output={"changed": changed},
        )


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
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=str(cwd),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            process.communicate(), timeout=context.settings.shell_timeout_seconds
        )
    except TimeoutError:
        process.kill()
        await process.wait()
        return ToolResult(tool_name=tool_name, ok=False, error="command timed out")
    return ToolResult(
        tool_name=tool_name,
        ok=process.returncode == 0,
        output={
            "argv": argv,
            "returncode": process.returncode,
            "stdout": stdout.decode("utf-8", errors="replace")[-12000:],
            "stderr": stderr.decode("utf-8", errors="replace")[-12000:],
        },
    )

