from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from synode.fabricator.common import FabricatorError

NAVIGATION_EVIDENCE_JSON = "navigation-evidence.json"
NAVIGATION_EVIDENCE_MARKDOWN = "navigation-evidence.md"


def refresh_navigation_evidence(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    status = navigation_evidence_status(run_dir, run)
    run["navigation_evidence"] = status
    return status


def ensure_navigation_evidence_ready(run_dir: Path, run: dict[str, Any]) -> None:
    status = refresh_navigation_evidence(run_dir, run)
    if status["required"] and not status["ready"]:
        raise FabricatorError(f"navigation evidence is required before this command: {status['error']}")


def navigation_evidence_status(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    required = bool(run.get("navigation_evidence_required"))
    path = run_dir / NAVIGATION_EVIDENCE_JSON
    result = {
        "required": required,
        "ready": not required,
        "status": "optional" if not required else "missing",
        "path": NAVIGATION_EVIDENCE_JSON,
        "markdown_path": NAVIGATION_EVIDENCE_MARKDOWN,
        "error": None,
    }
    if not path.exists():
        if required:
            result["error"] = f"{NAVIGATION_EVIDENCE_JSON} is missing"
        return result
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        result["status"] = "invalid"
        result["error"] = f"{NAVIGATION_EVIDENCE_JSON} is invalid JSON: {exc.msg}" if required else None
        return result
    if not required:
        result["status"] = str(payload.get("status") or "optional")
        result["ready"] = True
        return result
    errors = validate_navigation_evidence_payload(payload)
    result["status"] = str(payload.get("status") or "missing")
    result["ready"] = not errors
    result["error"] = "; ".join(errors) if errors else None
    return result


def validate_navigation_evidence_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["navigation evidence JSON must be an object"]
    errors: list[str] = []
    if payload.get("schema_version") != 1:
        errors.append("schema_version must be 1")
    if payload.get("status") != "complete":
        errors.append("status must be complete")
    for field in ("mcp_tools_used", "fallback_commands", "not_used", "findings"):
        if not is_string_list(payload.get(field)):
            errors.append(f"{field} must be a list of strings")
    mcp_tools = payload.get("mcp_tools_used") if isinstance(payload.get("mcp_tools_used"), list) else []
    fallbacks = payload.get("fallback_commands") if isinstance(payload.get("fallback_commands"), list) else []
    findings = payload.get("findings") if isinstance(payload.get("findings"), list) else []
    if not mcp_tools and not fallbacks:
        errors.append("record at least one MCP tool call or fallback command")
    if not findings:
        errors.append("record at least one concrete finding")
    return errors


def is_string_list(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) and item for item in value)
