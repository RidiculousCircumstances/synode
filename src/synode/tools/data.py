from __future__ import annotations

import csv
import json
import math
from statistics import mean
from typing import Any

from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext


class DataProfileTool:
    name = "native.data_profile"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        path_arg = arguments.get("path")
        if path_arg:
            path = context.workspace_policy.resolve_path(context.workspace, str(path_arg))
        else:
            root = context.workspace_policy.resolve_workspace(context.workspace)
            candidates = sorted([*root.rglob("*.csv"), *root.rglob("*.json")])
            if not candidates:
                return ToolResult(tool_name=self.name, ok=False, error="no CSV or JSON files found in workspace")
            path = candidates[0]

        if path.suffix.lower() == ".csv":
            return await self._profile_csv(path)
        if path.suffix.lower() == ".json":
            return await self._profile_json(path)
        return ToolResult(tool_name=self.name, ok=False, error=f"unsupported data file: {path}")

    async def _profile_csv(self, path: Any) -> ToolResult:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
        columns = list(rows[0].keys()) if rows else []
        numeric: dict[str, list[float]] = {column: [] for column in columns}
        missing: dict[str, int] = {column: 0 for column in columns}
        for row in rows:
            for column in columns:
                value = row.get(column)
                if value in {None, ""}:
                    missing[column] += 1
                    continue
                try:
                    number = float(str(value))
                except ValueError:
                    continue
                if math.isfinite(number):
                    numeric[column].append(number)
        numeric_summary = {
            column: {
                "count": len(values),
                "min": min(values),
                "max": max(values),
                "mean": mean(values),
            }
            for column, values in numeric.items()
            if values
        }
        return ToolResult(
            tool_name=self.name,
            ok=True,
            output={
                "path": str(path),
                "format": "csv",
                "rows": len(rows),
                "columns": columns,
                "missing": missing,
                "numeric_summary": numeric_summary,
            },
        )

    async def _profile_json(self, path: Any) -> ToolResult:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            sample = data[:5]
            rows = len(data)
        else:
            sample = data
            rows = 1
        return ToolResult(
            tool_name=self.name,
            ok=True,
            output={"path": str(path), "format": "json", "rows": rows, "sample": sample},
        )


class PythonSandboxTool:
    name = "native.python_sandbox"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.WRITE

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        code = str(arguments.get("code", ""))
        if not code.strip():
            return ToolResult(
                tool_name=self.name,
                ok=False,
                risk=ToolRisk.WRITE,
                error="code is required",
            )
        cwd = context.workspace_policy.resolve_workspace(context.workspace)
        result = await context.sandbox.run_python(
            code,
            cwd=cwd,
            timeout=float(arguments.get("timeout", context.settings.shell_timeout_seconds)),
        )
        return ToolResult(
            tool_name=self.name,
            ok=result.ok,
            risk=ToolRisk.WRITE,
            output={
                "argv": result.argv,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            },
            error=result.error,
        )
