from __future__ import annotations

from pathlib import Path
from typing import Any

from synode.fabricator import fabricator
from synode.fabricator.common import FabricatorError

SAFE_ADVANCE_COMMANDS = {
    "dispatch-experts",
    "validate-responses --phase expert",
    "synthesize",
    "dispatch-challenge",
    "validate-responses --phase challenge",
    "dispatch-review",
    "validate-responses --phase review",
    "finalize --result ready-for-human-review",
}


def advance(run_id: str, runs_dir: Path) -> dict[str, Any]:
    guide = fabricator.next_step(run_id, runs_dir)
    command = guide.get("command")
    if command not in SAFE_ADVANCE_COMMANDS:
        return {
            "ok": True,
            "run_id": run_id,
            "advanced": False,
            "manual_action_required": True,
            "blocked_reason": guide.get("blocked_reason"),
            "next": guide,
        }
    if command == "dispatch-experts":
        result = fabricator.dispatch_experts(run_id, runs_dir)
    elif command == "validate-responses --phase expert":
        result = fabricator.validate_responses(run_id, runs_dir, "expert")
    elif command == "synthesize":
        result = fabricator.synthesize(run_id, runs_dir)
    elif command == "dispatch-challenge":
        result = fabricator.dispatch_challenge(run_id, runs_dir)
    elif command == "validate-responses --phase challenge":
        result = fabricator.validate_responses(run_id, runs_dir, "challenge")
    elif command == "dispatch-review":
        result = fabricator.dispatch_review(run_id, runs_dir)
    elif command == "validate-responses --phase review":
        result = fabricator.validate_responses(run_id, runs_dir, "review")
    elif command == "finalize --result ready-for-human-review":
        result = fabricator.finalize_run(run_id, runs_dir, "ready-for-human-review")
    else:  # pragma: no cover - guarded by SAFE_ADVANCE_COMMANDS.
        raise FabricatorError(f"unsupported advance command: {command}")
    return {
        "ok": True,
        "run_id": run_id,
        "advanced": True,
        "command": command,
        "result": result,
        "next": fabricator.next_step(run_id, runs_dir),
    }
