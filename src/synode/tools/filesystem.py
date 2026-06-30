from __future__ import annotations

import re
from typing import Any

from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext
from synode.tools.mutations import run_sandboxed_file_write


class FileReadTool:
    name = "native.fs_read"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        path = context.workspace_policy.resolve_path(context.workspace, str(arguments["path"]))
        max_bytes = int(arguments.get("max_bytes", 12000))
        if not path.exists():
            return ToolResult(tool_name=self.name, ok=False, error=f"file not found: {path}")
        if not path.is_file():
            return ToolResult(tool_name=self.name, ok=False, error=f"path is not a file: {path}")
        data = path.read_bytes()[:max_bytes]
        return ToolResult(
            tool_name=self.name,
            ok=True,
            output={"path": str(path), "content": data.decode("utf-8", errors="replace"), "truncated": path.stat().st_size > max_bytes},
        )


class FileSearchTool:
    name = "native.fs_search"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        root = context.workspace_policy.resolve_workspace(context.workspace)
        pattern = str(arguments.get("pattern", ""))
        glob = str(arguments.get("glob", "*"))
        max_matches = int(arguments.get("max_matches", 50))
        regex = re.compile(pattern, re.IGNORECASE) if pattern else None
        matches: list[dict[str, Any]] = []
        for path in root.rglob(glob):
            if len(matches) >= max_matches:
                break
            if not path.is_file() or ".git" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if regex is None:
                matches.append({"path": str(path.relative_to(root)), "line": None, "text": ""})
                continue
            for line_number, line in enumerate(text.splitlines(), start=1):
                if regex.search(line):
                    matches.append({"path": str(path.relative_to(root)), "line": line_number, "text": line[:240]})
                    break
        return ToolResult(tool_name=self.name, ok=True, output={"root": str(root), "matches": matches})


class FileWriteTool:
    name = "native.fs_write"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.WRITE

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        context.sandbox.ensure_available()
        return await run_sandboxed_file_write(
            context,
            raw_path=str(arguments["path"]),
            content=str(arguments.get("content", "")),
        )
