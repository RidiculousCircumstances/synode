from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import Any

from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext
from synode.tools.sandbox import SandboxResult

_PAYLOAD_ENV = "SYNODE_SANDBOX_MUTATION_PAYLOAD_B64"
_MUTATION_SCRIPT = r"""
from __future__ import annotations

import base64
import hashlib
import json
import os
import sys
from pathlib import Path


def emit(payload: dict, code: int = 0) -> None:
    sys.stdout.write(json.dumps(payload, separators=(",", ":")))
    sys.exit(code)


def fail(message: str, code: int = 2) -> None:
    emit({"ok": False, "error": message}, code)


def resolve_relative(root: Path, raw_path: str) -> Path:
    if not raw_path or raw_path.startswith("/") or "\x00" in raw_path:
        fail(f"path must be relative to workspace: {raw_path}")
    target = (root / raw_path).resolve(strict=False)
    try:
        target.relative_to(root)
    except ValueError:
        fail(f"path escapes workspace: {raw_path}")
    return target


root = Path.cwd().resolve()
try:
    payload = json.loads(base64.b64decode(os.environ["SYNODE_SANDBOX_MUTATION_PAYLOAD_B64"]).decode("utf-8"))
except Exception as exc:
    fail(f"invalid mutation payload: {exc}")

operation = payload.get("operation")
if operation == "write_file":
    path = resolve_relative(root, str(payload.get("path", "")))
    content = str(payload.get("content", ""))
    encoded = content.encode("utf-8")
    max_bytes = int(payload.get("max_bytes", 0))
    if max_bytes > 0 and len(encoded) > max_bytes:
        fail(f"file content exceeds sandbox mutation payload limit of {max_bytes} bytes")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    emit({"ok": True, "path": str(path.relative_to(root)), "bytes": len(encoded)})

if operation == "patch_apply":
    patches = payload.get("patches")
    if not isinstance(patches, list) or not patches:
        fail("patches are required")
    targets = {}
    target_order = []
    for item in patches:
        if not isinstance(item, dict):
            fail("patch item must be an object")
        path = resolve_relative(root, str(item.get("path", "")))
        if not path.exists() or not path.is_file():
            fail(f"patch target is not an existing file: {path.relative_to(root)}")
        relative_path = str(path.relative_to(root))
        if relative_path not in targets:
            old_content = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(old_content.encode("utf-8")).hexdigest()
            targets[relative_path] = {
                "path": path,
                "old_content": old_content,
                "new_content": old_content,
                "old_sha256": digest,
            }
            target_order.append(relative_path)
        target = targets[relative_path]
        expected_sha256 = str(item.get("expected_sha256", ""))
        if target["old_sha256"] != expected_sha256:
            fail(f"checksum mismatch for {relative_path}: expected {expected_sha256}, got {target['old_sha256']}")
        old_text = str(item.get("old_text", ""))
        new_text = str(item.get("new_text", ""))
        current_content = target["new_content"]
        occurrences = current_content.count(old_text)
        if occurrences != 1:
            fail(f"old_text must occur exactly once in {relative_path}; occurrences={occurrences}")
        target["new_content"] = current_content.replace(old_text, new_text, 1)
    changed = []
    written = []
    try:
        for relative_path in target_order:
            target = targets[relative_path]
            target["path"].write_text(target["new_content"], encoding="utf-8")
            written.append(relative_path)
            changed.append({"path": relative_path, "old_sha256": target["old_sha256"]})
    except OSError as exc:
        for relative_path in written:
            target = targets[relative_path]
            try:
                target["path"].write_text(target["old_content"], encoding="utf-8")
            except OSError:
                pass
        fail(f"failed to write patch result: {exc}")
    emit({"ok": True, "changed": changed})

fail(f"unsupported mutation operation: {operation}")
"""


async def run_sandboxed_file_write(context: ToolContext, *, raw_path: str, content: str) -> ToolResult:
    root = context.workspace_policy.resolve_workspace(context.workspace)
    path = context.workspace_policy.resolve_path(context.workspace, raw_path)
    relative_path = _relative_to_workspace(root, path)
    payload = {
        "operation": "write_file",
        "path": relative_path,
        "content": content,
        "max_bytes": _mutation_payload_limit(context),
    }
    payload_bytes = _encode_payload(payload)
    if len(payload_bytes) > _mutation_payload_limit(context):
        return ToolResult(
            tool_name="native.fs_write",
            ok=False,
            risk=ToolRisk.WRITE,
            error=f"mutation payload exceeds limit of {_mutation_payload_limit(context)} bytes",
            output=_diagnostic_output(context, payload_bytes, None),
        )
    result = await _run_mutation(context, root, payload_bytes)
    parsed = _parse_mutation_stdout(result)
    output = {
        **_diagnostic_output(context, payload_bytes, result),
        "path": str(path),
        "bytes": len(content.encode("utf-8")),
    }
    if not result.ok:
        return ToolResult(
            tool_name="native.fs_write",
            ok=False,
            risk=ToolRisk.WRITE,
            error=_mutation_error(result, parsed),
            output=output,
        )
    return ToolResult(tool_name="native.fs_write", ok=True, risk=ToolRisk.WRITE, output=output)


async def run_sandboxed_patch_apply(context: ToolContext, *, patches: list[Any]) -> ToolResult:
    if not patches:
        return ToolResult(tool_name="native.patch_apply", ok=False, risk=ToolRisk.WRITE, error="patches are required")
    root = context.workspace_policy.resolve_workspace(context.workspace)
    sandbox_patches: list[dict[str, str]] = []
    for patch in patches:
        if not isinstance(patch, dict):
            return ToolResult(
                tool_name="native.patch_apply",
                ok=False,
                risk=ToolRisk.WRITE,
                error="patch item must be an object",
            )
        try:
            path = context.workspace_policy.resolve_path(context.workspace, str(patch["path"]))
            expected_sha256 = str(patch["expected_sha256"])
            old_text = str(patch["old_text"])
            new_text = str(patch["new_text"])
        except KeyError as exc:
            return ToolResult(
                tool_name="native.patch_apply",
                ok=False,
                risk=ToolRisk.WRITE,
                error=f"patch field is required: {exc.args[0]}",
            )
        sandbox_patches.append(
            {
                "path": _relative_to_workspace(root, path),
                "expected_sha256": expected_sha256,
                "old_text": old_text,
                "new_text": new_text,
            }
        )
    payload = {"operation": "patch_apply", "patches": sandbox_patches}
    payload_bytes = _encode_payload(payload)
    if len(payload_bytes) > _mutation_payload_limit(context):
        return ToolResult(
            tool_name="native.patch_apply",
            ok=False,
            risk=ToolRisk.WRITE,
            error=f"mutation payload exceeds limit of {_mutation_payload_limit(context)} bytes",
            output=_diagnostic_output(context, payload_bytes, None),
        )
    result = await _run_mutation(context, root, payload_bytes)
    parsed = _parse_mutation_stdout(result)
    output = {
        **_diagnostic_output(context, payload_bytes, result),
        "changed": [
            {"path": str(root / item["path"]), "old_sha256": item["old_sha256"]}
            for item in parsed.get("changed", [])
            if isinstance(item, dict) and "path" in item and "old_sha256" in item
        ],
    }
    if not result.ok:
        return ToolResult(
            tool_name="native.patch_apply",
            ok=False,
            risk=ToolRisk.WRITE,
            error=_mutation_error(result, parsed),
            output=output,
        )
    return ToolResult(tool_name="native.patch_apply", ok=True, risk=ToolRisk.WRITE, output=output)


async def _run_mutation(context: ToolContext, root: Path, payload_bytes: bytes) -> SandboxResult:
    return await context.sandbox.run_python(
        _MUTATION_SCRIPT,
        cwd=root,
        timeout=context.settings.shell_timeout_seconds,
        env={_PAYLOAD_ENV: base64.b64encode(payload_bytes).decode("ascii")},
    )


def _encode_payload(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _parse_mutation_stdout(result: SandboxResult) -> dict[str, Any]:
    try:
        parsed = json.loads(result.stdout or "{}")
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _mutation_error(result: SandboxResult, parsed: dict[str, Any]) -> str:
    if result.error:
        return result.error
    if isinstance(parsed.get("error"), str):
        return str(parsed["error"])
    return result.stderr.strip() or f"sandbox mutation failed with return code {result.returncode}"


def _diagnostic_output(
    context: ToolContext,
    payload_bytes: bytes,
    result: SandboxResult | None,
) -> dict[str, Any]:
    status = context.sandbox.status()
    output: dict[str, Any] = {
        "sandbox_backend": status.backend,
        "sandbox_execution": "sandbox_runner",
        "payload_bytes": len(payload_bytes),
    }
    if result is not None:
        output.update(
            {
                "argv": result.argv,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
    return output


def _mutation_payload_limit(context: ToolContext) -> int:
    return min(context.settings.max_artifact_payload_bytes, context.settings.sandbox_disk_mb * 1024 * 1024)


def _relative_to_workspace(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()
