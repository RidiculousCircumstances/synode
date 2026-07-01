from __future__ import annotations

import re
from typing import Any

from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext
from synode.tools.mutations import run_sandboxed_file_write


def _unexpected_arguments(arguments: dict[str, Any], allowed: set[str]) -> list[str]:
    return sorted(key for key in arguments if key not in allowed)


def _positive_int_argument(arguments: dict[str, Any], name: str, default: int, maximum: int) -> int:
    raw_value = arguments.get(name, default)
    value = int(raw_value)
    if value < 1:
        return 1
    return min(value, maximum)


def _glob_is_unsafe(glob: str) -> bool:
    return glob.startswith("/") or ".." in glob.split("/")


def _looks_like_file_glob(pattern: str) -> bool:
    stripped = pattern.strip()
    return stripped.startswith("*.") or stripped.startswith("**/") or ("/" in stripped and "*" in stripped)


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


class FileListTool:
    name = "native.fs_list"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        unexpected = _unexpected_arguments(arguments, {"glob", "max_matches"})
        if unexpected:
            return ToolResult(
                tool_name=self.name,
                ok=False,
                error=(
                    f"unexpected arguments for {self.name}: {', '.join(unexpected)}. "
                    "The workspace root is implicit; use glob for file selection."
                ),
                output={"allowed_arguments": ["glob", "max_matches"]},
            )
        root = context.workspace_policy.resolve_workspace(context.workspace)
        glob = str(arguments.get("glob") or "*")
        if _glob_is_unsafe(glob):
            return ToolResult(
                tool_name=self.name,
                ok=False,
                error="invalid glob: use a relative workspace glob without '..'",
                output={"glob": glob},
            )
        max_matches = _positive_int_argument(arguments, "max_matches", 200, 500)
        matches: list[dict[str, Any]] = []
        for path in root.rglob(glob):
            if len(matches) >= max_matches:
                break
            if ".git" in path.parts or not path.is_file():
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            matches.append({"path": str(path.relative_to(root)), "size": size})
        return ToolResult(tool_name=self.name, ok=True, output={"root": str(root), "matches": matches})


class FileSearchTool:
    name = "native.fs_search"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        unexpected = _unexpected_arguments(arguments, {"pattern", "glob", "max_matches"})
        if unexpected:
            return ToolResult(
                tool_name=self.name,
                ok=False,
                error=(
                    f"unexpected arguments for {self.name}: {', '.join(unexpected)}. "
                    "Use native.fs_list for file listing. The workspace root is implicit."
                ),
                output={
                    "allowed_arguments": ["pattern", "glob", "max_matches"],
                    "suggested_call": {"name": "native.fs_list", "arguments": {"glob": str(arguments.get("glob") or "*")}},
                },
            )
        root = context.workspace_policy.resolve_workspace(context.workspace)
        raw_pattern = arguments.get("pattern")
        pattern = str(raw_pattern or "")
        glob = str(arguments.get("glob", "*"))
        if not pattern:
            return ToolResult(
                tool_name=self.name,
                ok=False,
                error="pattern is required for native.fs_search; use native.fs_list to list files",
                output={
                    "allowed_arguments": ["pattern", "glob", "max_matches"],
                    "suggested_call": {"name": "native.fs_list", "arguments": {"glob": glob}},
                },
            )
        if _glob_is_unsafe(glob):
            return ToolResult(
                tool_name=self.name,
                ok=False,
                error="invalid glob: use a relative workspace glob without '..'",
                output={"glob": glob},
            )
        if _looks_like_file_glob(pattern):
            return ToolResult(
                tool_name=self.name,
                ok=False,
                error="pattern is a regex for file contents, not a file glob; put file globs in glob or use native.fs_list",
                output={
                    "pattern": pattern,
                    "glob": glob,
                    "suggested_call": {"name": "native.fs_list", "arguments": {"glob": pattern}},
                },
            )
        max_matches = _positive_int_argument(arguments, "max_matches", 50, 500)
        try:
            regex = re.compile(pattern, re.IGNORECASE)
        except re.error as exc:
            return ToolResult(
                tool_name=self.name,
                ok=False,
                error=f"invalid regex pattern: {exc}; use glob for file globs and pattern for text regex",
                output={"pattern": pattern, "glob": glob},
            )
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
