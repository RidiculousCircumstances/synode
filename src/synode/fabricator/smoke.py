from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from synode.fabricator import council, fabricator
from synode.fabricator.common import FabricatorError
from synode.fabricator.rendering import write_json


def smoke(runs_dir: Path) -> dict[str, Any]:
    assert_safe_smoke_dir(runs_dir)
    if runs_dir.exists():
        shutil.rmtree(runs_dir)
    result = fabricator.validate_all()
    started = fabricator.start_run(
        mode="plan-patch",
        goal="Fabricator smoke run",
        paths=["docs/fabricator/protocol.md"],
        profile_id="docs_process",
        run_id="smoke",
        runs_dir=runs_dir,
    )
    status = fabricator.status_run("smoke", runs_dir)
    next_after_start = fabricator.next_step("smoke", runs_dir)
    smoke_navigation = complete_smoke_navigation(runs_dir / "smoke")
    expert_dispatch = fabricator.dispatch_experts("smoke", runs_dir)
    smoke_run = fabricator.read_run(runs_dir / "smoke")
    smoke_expert = smoke_run["optional_experts"][0]
    agent_start = fabricator.agent_started("smoke", runs_dir, "expert", smoke_expert, "smoke-agent")
    council.write_smoke_responses(runs_dir / "smoke", fabricator.read_run(runs_dir / "smoke"))
    response_validation = fabricator.validate_responses("smoke", runs_dir, "expert")
    synthesis_result = fabricator.synthesize("smoke", runs_dir)
    smoke_traceability = accept_smoke_traceability(runs_dir / "smoke")
    decision_ready = fabricator.mark_decision_ready("smoke", runs_dir)
    implementation_ready = fabricator.mark_implementation_ready("smoke", runs_dir)
    review_dispatch = fabricator.dispatch_review("smoke", runs_dir)
    fabricator.agent_started("smoke", runs_dir, "review", review_dispatch["reviewer"], "smoke-review-agent")
    council.write_smoke_responses(runs_dir / "smoke", fabricator.read_run(runs_dir / "smoke"), phase="review")
    review_validation = fabricator.validate_responses("smoke", runs_dir, "review")
    summary_result = fabricator.summarize("smoke", runs_dir)
    finalized = fabricator.finalize_run("smoke", runs_dir, "ready-for-human-review")
    return {
        "ok": True,
        "validate": result,
        "start": started,
        "status": status,
        "next": next_after_start,
        "navigation": smoke_navigation,
        "expert_dispatch": expert_dispatch,
        "agent_start": agent_start,
        "response_validation": response_validation,
        "synthesis": synthesis_result,
        "traceability": smoke_traceability,
        "decision_ready": decision_ready,
        "implementation_ready": implementation_ready,
        "review_dispatch": review_dispatch,
        "review_validation": review_validation,
        "summary": summary_result,
        "finalize": finalized,
    }


def complete_smoke_navigation(run_dir: Path) -> dict[str, Any]:
    payload = {
        "schema_version": 1,
        "status": "complete",
        "required": True,
        "mcp_tools_used": [],
        "fallback_commands": ["uv run synode fabricator validate"],
        "not_used": ["Smoke run uses local fixture state only."],
        "findings": ["Fabricator smoke scope is docs_process over docs/fabricator/protocol.md."],
        "notes": [],
    }
    write_json(run_dir / "navigation-evidence.json", payload)
    (run_dir / "navigation-evidence.md").write_text(
        "\n".join(
            [
                "# Fabricator Navigation Evidence",
                "",
                "Run ID: `smoke`",
                "Required: `true`",
                "",
                "## MCP Tools Used",
                "",
                "- None.",
                "",
                "## Fallback Commands",
                "",
                "- `uv run synode fabricator validate`",
                "",
                "## Not Used And Why",
                "",
                "- Smoke run uses local fixture state only.",
                "",
                "## Findings",
                "",
                "- Fabricator smoke scope is docs_process over docs/fabricator/protocol.md.",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return {"ok": True, "status": "complete"}


def accept_smoke_traceability(run_dir: Path) -> dict[str, Any]:
    run = fabricator.read_run(run_dir)
    trace = council.trace_decision(run_dir, run)
    path = run_dir / "decision-traceability.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    item_ids = []
    for item in payload["items"]:
        item["resolution"] = "accepted"
        item["resolution_reason"] = "Smoke run accepts generated smoke-only findings."
        item["evidence"] = "Smoke run."
        item_ids.append(item["id"])
    payload["unresolved"] = []
    write_json(path, payload)
    if item_ids:
        references = "\n".join(f"- Traceability item `{item_id}` accepted for smoke." for item_id in item_ids)
        for name in ("arbiter-decision.md", "implementation-handoff.md"):
            artifact = run_dir / name
            artifact.write_text(
                f"{artifact.read_text(encoding='utf-8').rstrip()}\n\n## Decision Traceability\n\n{references}\n",
                encoding="utf-8",
            )
    return {"ok": True, "items": trace["items"], "accepted": item_ids}


def assert_safe_smoke_dir(path: Path) -> None:
    resolved = path.resolve()
    tmp_root = Path("/tmp").resolve()
    if not is_relative_to(resolved, tmp_root):
        raise FabricatorError("smoke runs_dir must be under /tmp")


def is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return False
    return True
