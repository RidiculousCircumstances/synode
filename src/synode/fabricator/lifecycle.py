from __future__ import annotations

import datetime as dt
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from synode.fabricator.common import FabricatorError
from synode.fabricator.rendering import write_json

STATE_MACHINE_VERSION = "v3.7"
TERMINAL_STATUSES = {"completed", "blocked", "cancelled"}
LOCK_FILE_NAME = ".fabricator.lock"
PHASE_DEADLINE_MINUTES = {"expert": 45, "challenge": 30, "review": 30}
PHASE_STATUS = {
    "expert": "expert-agents-started",
    "challenge": "challenge-agents-started",
    "review": "review-agent-started",
}
DISPATCH_STATUS = {
    "expert": "expert-dispatch-ready",
    "challenge": "challenge-dispatch-ready",
    "review": "review-dispatch-ready",
}
RESPONSE_COLLECTED_STATUS = {
    "expert": "expert-responses-collected",
    "challenge": "challenge-responses-collected",
    "review": "review-responses-collected",
}
COMMAND_ALLOWED_STATUSES = {
    "dispatch-experts": {"started"},
    "validate-responses:expert": {"expert-dispatch-ready", "expert-agents-started"},
    "synthesize": {"expert-responses-collected"},
    "dispatch-challenge": {"synthesis-ready"},
    "validate-responses:challenge": {"challenge-dispatch-ready", "challenge-agents-started"},
    "mark-decision-ready": {"synthesis-ready", "challenge-responses-collected"},
    "mark-implementation-ready": {"decision-ready"},
    "dispatch-review": {"decision-ready", "implementation-ready"},
    "validate-responses:review": {"review-dispatch-ready", "review-agent-started", "review-responses-collected"},
    "finalize": {"decision-ready", "review-responses-collected", "implementation-ready"},
}
AGENT_START_ALLOWED_STATUSES = {
    "expert": {"expert-dispatch-ready", "expert-agents-started"},
    "challenge": {"challenge-dispatch-ready", "challenge-agents-started"},
    "review": {"review-dispatch-ready", "review-agent-started", "review-responses-collected"},
}


def initialize_run(run: dict[str, Any]) -> None:
    run["state_machine_version"] = STATE_MACHINE_VERSION
    run["current_phase"] = "intake"
    run["allowed_next_commands"] = ["dispatch-experts"]
    run["agent_deadline_minutes"] = dict(PHASE_DEADLINE_MINUTES)
    run["agents"] = {}
    run["failures"] = []
    run["last_next_action"] = None
    after_status_change(run)


@contextmanager
def run_lock(run_dir: Path, command: str):
    run_dir.mkdir(parents=True, exist_ok=True)
    lock_path = run_dir / LOCK_FILE_NAME
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        details = lock_path.read_text(encoding="utf-8") if lock_path.exists() else "unknown owner"
        raise FabricatorError(
            f"Fabricator run is locked by another mutation: {details.strip()}"
        ) from exc
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(f"command={command}\ncreated_at={utc_now()}\npid={os.getpid()}\n")
        yield
    finally:
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def ensure_command_allowed(run: dict[str, Any], command: str) -> None:
    status = run["status"]
    if status in TERMINAL_STATUSES:
        raise FabricatorError(f"run is terminal ({status}); command {command} is not allowed")
    allowed = COMMAND_ALLOWED_STATUSES.get(command)
    if allowed is None:
        return
    if status not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise FabricatorError(f"command {command} requires status: {allowed_text}; current status is {status}")


def after_status_change(run: dict[str, Any]) -> None:
    guide = build_next_action(run)
    run["current_phase"] = guide["phase"]
    run["allowed_next_commands"] = guide["allowed_commands"]
    run["last_next_action"] = guide


def next_action(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    failures = collect_failures(run_dir, run, persist=True)
    guide = build_next_action(run, failures=failures)
    run["current_phase"] = guide["phase"]
    run["allowed_next_commands"] = guide["allowed_commands"]
    run["last_next_action"] = guide
    run["updated_at"] = utc_now()
    write_json(run_dir / "run.json", run)
    write_lifecycle(run_dir, run)
    return {"ok": True, **guide}


def summarize(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    failures = collect_failures(run_dir, run, persist=True)
    guide = build_next_action(run, failures=failures)
    outstanding = list_outstanding_agents(run)
    summary = {
        "run_id": run["id"],
        "status": run["status"],
        "phase": guide["phase"],
        "next_action": guide["action"],
        "next_command": guide.get("command"),
        "outstanding_agents": outstanding,
        "failures": failures,
        "blocked_reason": guide.get("blocked_reason"),
        "unresolved_traceability": guide.get("unresolved_traceability", []),
        "review_blockers": guide.get("review_blockers", []),
        "challenge_required": run.get("challenge_required", False),
        "challenge_candidates": run.get("synthesis", {}).get("challenge_candidates", []),
        "files_to_read": guide["files_to_read"],
    }
    (run_dir / "summary.md").write_text(render_summary(summary), encoding="utf-8")
    write_json(run_dir / "summary.json", summary)
    write_lifecycle(run_dir, run)
    return {"ok": True, **summary}


def mark_agent_started(
    run_dir: Path,
    run: dict[str, Any],
    *,
    phase: str,
    expert_id: str,
    agent_id: str | None,
) -> dict[str, Any]:
    ensure_agent_event_allowed(run, phase)
    ensure_expected_agent(run, phase, expert_id)
    agent = ensure_agent_record(run, phase, expert_id)
    now = utc_now()
    agent["status"] = "started"
    agent["agent_id"] = agent_id
    agent["started_at"] = now
    agent["deadline_at"] = agent.get("deadline_at") or add_minutes(now, deadline_minutes(run, phase))
    run["status"] = PHASE_STATUS[phase]
    run["updated_at"] = now
    after_status_change(run)
    write_json(run_dir / "run.json", run)
    write_lifecycle(run_dir, run)
    return {"ok": True, "run_id": run["id"], "phase": phase, "expert_id": expert_id, "status": "started"}


def mark_agent_completed(
    run_dir: Path,
    run: dict[str, Any],
    *,
    phase: str,
    expert_id: str,
) -> dict[str, Any]:
    ensure_agent_event_allowed(run, phase)
    ensure_expected_agent(run, phase, expert_id)
    ensure_response_artifacts_exist(run_dir, run, phase, expert_id)
    agent = ensure_agent_record(run, phase, expert_id)
    agent["status"] = "completed"
    agent["completed_at"] = utc_now()
    clear_agent_failures(
        run_dir,
        run,
        phase,
        expert_id,
        {"invalid_response", "missing_response", "overdue_agent"},
    )
    run["updated_at"] = agent["completed_at"]
    after_status_change(run)
    write_json(run_dir / "run.json", run)
    write_lifecycle(run_dir, run)
    return {"ok": True, "run_id": run["id"], "phase": phase, "expert_id": expert_id, "status": "completed"}


def mark_agent_failed(
    run_dir: Path,
    run: dict[str, Any],
    *,
    phase: str,
    expert_id: str,
    reason: str,
) -> dict[str, Any]:
    ensure_agent_event_allowed(run, phase)
    ensure_expected_agent(run, phase, expert_id)
    agent = ensure_agent_record(run, phase, expert_id)
    now = utc_now()
    agent["status"] = "failed"
    agent["failed_at"] = now
    agent["failure_reason"] = reason
    failure = record_failure(run_dir, run, phase, expert_id, "failed_agent", reason)
    run["updated_at"] = now
    after_status_change(run)
    write_json(run_dir / "run.json", run)
    write_lifecycle(run_dir, run)
    return {"ok": True, "run_id": run["id"], "phase": phase, "expert_id": expert_id, "failure": failure}


def mark_decision_ready(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    ensure_command_allowed(run, "mark-decision-ready")
    run["status"] = "decision-ready"
    run["updated_at"] = utc_now()
    after_status_change(run)
    write_json(run_dir / "run.json", run)
    write_lifecycle(run_dir, run)
    return {"ok": True, "run_id": run["id"], "status": run["status"]}


def mark_implementation_ready(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    ensure_command_allowed(run, "mark-implementation-ready")
    run["status"] = "implementation-ready"
    run["updated_at"] = utc_now()
    after_status_change(run)
    write_json(run_dir / "run.json", run)
    write_lifecycle(run_dir, run)
    return {"ok": True, "run_id": run["id"], "status": run["status"]}


def agent_timeouts(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    failures = collect_failures(run_dir, run, persist=True)
    write_lifecycle(run_dir, run)
    overdue = [failure for failure in failures if failure["kind"] == "overdue_agent"]
    return {"ok": True, "run_id": run["id"], "overdue": overdue, "failures": failures}


def register_dispatch(run: dict[str, Any], phase: str, expert_ids: list[str], created_at: str) -> None:
    agents = run.setdefault("agents", {})
    phase_agents = agents.setdefault(phase, {})
    for expert_id in expert_ids:
        phase_agents[expert_id] = {
            "expert_id": expert_id,
            "phase": phase,
            "status": "expected",
            "dispatch_created_at": created_at,
            "deadline_at": add_minutes(created_at, deadline_minutes(run, phase)),
            "agent_id": None,
            "started_at": None,
            "completed_at": None,
            "failed_at": None,
            "failure_reason": None,
        }


def build_next_action(run: dict[str, Any], *, failures: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    failures = failures or []
    if failures:
        return guide(
            run,
            "blocked",
            "Resolve recorded failures before continuing.",
            None,
            [],
            ["run.json", "summary.md"],
            blocked_reason="unresolved_failures",
            unresolved_failures=failures,
        )
    if run.get("review_blockers"):
        return guide(
            run,
            "blocked",
            "Resolve review blocker findings before finalization.",
            None,
            [],
            ["review-report.md", "review-responses/"],
            blocked_reason="review_blockers",
        )

    status = run["status"]
    if status == "started":
        navigation = run.get("navigation_evidence", {})
        if navigation.get("required") and not navigation.get("ready"):
            return guide(
                run,
                "navigation-evidence",
                "Complete Fabricator navigation evidence before dispatching the Fabricator council.",
                None,
                [],
                ["navigation-evidence.md", "navigation-evidence.json", "selection.md"],
                blocked_reason="navigation_evidence_required",
            )
        return guide(run, "expert-dispatch", "Dispatch selected expert subagents.", "dispatch-experts", ["dispatch-experts"], ["selection.md"])
    if status == "expert-dispatch-ready":
        return guide(run, "expert-agents", "Start listed expert subagents and record agent-started events.", None, [], ["dispatch/expert-agents.md"])
    if status == "expert-agents-started":
        return guide(run, "expert-responses", "Wait for expert responses, then validate them.", "validate-responses --phase expert", ["validate-responses:expert"], ["expert-responses/"])
    if status == "expert-responses-collected":
        return guide(run, "synthesis", "Build Arbiter synthesis.", "synthesize", ["synthesize"], ["expert-responses/"])
    if status == "synthesis-ready":
        if run.get("challenge_required"):
            return guide(
                run,
                "challenge-dispatch",
                "Read decision brief, then dispatch targeted challenge subagents.",
                "dispatch-challenge",
                ["dispatch-challenge"],
                ["decision-brief.md", "finding-clusters.md", "arbiter-synthesis.md", "challenge-brief.md"],
            )
        return guide(
            run,
            "arbiter-decision",
            "Read decision brief, then write Arbiter decision and implementation handoff.",
            None,
            [],
            ["decision-brief.md", "finding-clusters.md", "arbiter-decision.md", "implementation-handoff.md"],
        )
    if status == "challenge-dispatch-ready":
        return guide(run, "challenge-agents", "Start challenge subagents and record agent-started events.", None, [], ["dispatch/challenge-agents.md"])
    if status == "challenge-agents-started":
        return guide(run, "challenge-responses", "Wait for challenge responses, then validate them.", "validate-responses --phase challenge", ["validate-responses:challenge"], ["challenge-responses/"])
    if status == "challenge-responses-collected":
        return guide(
            run,
            "arbiter-decision",
            "Resolve challenge objections and write Arbiter decision.",
            None,
            [],
            ["decision-brief.md", "finding-clusters.md", "challenge-responses/", "arbiter-decision.md"],
        )
    if status == "decision-ready":
        if run["mode"] == "plan-only":
            return guide(
                run,
                "finalize",
                "Finalize plan-only run for human review.",
                "finalize --result ready-for-human-review",
                ["finalize"],
                ["arbiter-decision.md", "implementation-handoff.md"],
            )
        return guide(run, "implementation", "Implement within Arbiter handoff, then mark implementation ready.", None, [], ["implementation-handoff.md"])
    if status == "implementation-ready":
        return guide(run, "review-dispatch", "Dispatch selected reviewer.", "dispatch-review", ["dispatch-review"], ["review-report.md"])
    if status == "review-dispatch-ready":
        return guide(run, "review-agent", "Start reviewer subagent and record agent-started event.", None, [], ["dispatch/review-agent.md"])
    if status == "review-agent-started":
        return guide(run, "review-response", "Wait for review response, then validate it.", "validate-responses --phase review", ["validate-responses:review"], ["review-responses/"])
    if status == "review-responses-collected":
        return guide(run, "finalize", "Finalize for human review.", "finalize --result ready-for-human-review", ["finalize"], ["review-report.md", "final-summary.md"])
    if status in TERMINAL_STATUSES:
        return guide(run, "terminal", f"Run is terminal: {status}.", None, [], ["final-summary.md"])
    return guide(run, "manual", f"No automatic next action for status {status}; Arbiter decision required.", None, [], ["run.json"])


def collect_failures(run_dir: Path, run: dict[str, Any], *, persist: bool = False) -> list[dict[str, Any]]:
    failures = list(run.get("failures", []))
    now = dt.datetime.now(dt.timezone.utc)
    for phase, phase_agents in run.get("agents", {}).items():
        for expert_id, agent in phase_agents.items():
            if agent["status"] in {"completed", "failed"}:
                continue
            deadline_at = parse_time(agent["deadline_at"])
            if deadline_at and deadline_at < now and not has_failure(failures, phase, expert_id, "overdue_agent"):
                failure = build_failure(run, phase, expert_id, "overdue_agent", f"Agent deadline passed at {agent['deadline_at']}.")
                failures.append(failure)
                if persist:
                    write_failure(run_dir, failure)
    if persist and failures != run.get("failures", []):
        run["failures"] = failures
        run["updated_at"] = utc_now()
        write_json(run_dir / "run.json", run)
    return failures


def record_failure(run_dir: Path, run: dict[str, Any], phase: str, expert_id: str, kind: str, message: str) -> dict[str, Any]:
    failures = run.setdefault("failures", [])
    failure = build_failure(run, phase, expert_id, kind, message)
    failures.append(failure)
    write_failure(run_dir, failure)
    return failure


def clear_agent_failures(
    run_dir: Path,
    run: dict[str, Any],
    phase: str,
    expert_id: str,
    kinds: set[str],
) -> None:
    run["failures"] = [
        failure
        for failure in run.get("failures", [])
        if not (
            failure["phase"] == phase
            and failure["expert_id"] == expert_id
            and failure["kind"] in kinds
        )
    ]
    failure_dir = run_dir / "failures"
    for kind in kinds:
        try:
            (failure_dir / f"{phase}-{expert_id}-{kind}.json").unlink()
        except FileNotFoundError:
            pass


def write_lifecycle(run_dir: Path, run: dict[str, Any]) -> None:
    write_json(
        run_dir / "lifecycle.json",
        {
            "run_id": run["id"],
            "state_machine_version": run.get("state_machine_version"),
            "status": run["status"],
            "current_phase": run.get("current_phase"),
            "allowed_next_commands": run.get("allowed_next_commands", []),
            "navigation_evidence": run.get("navigation_evidence", {}),
            "agents": run.get("agents", {}),
            "failures": run.get("failures", []),
            "last_next_action": run.get("last_next_action"),
        },
    )


def build_failure(run: dict[str, Any], phase: str, expert_id: str, kind: str, message: str) -> dict[str, Any]:
    return {
        "run_id": run["id"],
        "phase": phase,
        "expert_id": expert_id,
        "kind": kind,
        "message": message,
        "created_at": utc_now(),
    }


def write_failure(run_dir: Path, failure: dict[str, Any]) -> None:
    failure_dir = run_dir / "failures"
    failure_dir.mkdir(parents=True, exist_ok=True)
    path = failure_dir / f"{failure['phase']}-{failure['expert_id']}-{failure['kind']}.json"
    write_json(path, failure)


def guide(
    run: dict[str, Any],
    phase: str,
    action: str,
    command: str | None,
    allowed_commands: list[str],
    files_to_read: list[str],
    *,
    blocked_reason: str | None = None,
    unresolved_failures: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run["id"],
        "status": run["status"],
        "phase": phase,
        "action": action,
        "command": command,
        "allowed_commands": allowed_commands,
        "files_to_read": files_to_read,
        "requires_arbiter": command is None,
        "manual_action_required": command is None,
        "blocked_reason": blocked_reason,
        "unresolved_traceability": run.get("decision_traceability", {}).get("unresolved", []),
        "unresolved_failures": unresolved_failures or [],
        "review_blockers": run.get("review_blockers", []),
        "navigation_evidence": run.get("navigation_evidence", {}),
        "resource_policy": run.get("resource_policy", {}),
        "resource_policy_effective": run.get("resource_policy_effective", {}),
        "resource_checks": run.get("resource_checks", []),
    }


def ensure_expected_agent(run: dict[str, Any], phase: str, expert_id: str) -> None:
    phase_agents = run.get("agents", {}).get(phase, {})
    if expert_id not in phase_agents:
        raise FabricatorError(f"{expert_id} is not an expected {phase} agent")


def ensure_agent_event_allowed(run: dict[str, Any], phase: str) -> None:
    if run["status"] in TERMINAL_STATUSES:
        raise FabricatorError(f"run is terminal ({run['status']}); agent events are not allowed")
    allowed = AGENT_START_ALLOWED_STATUSES[phase]
    if run["status"] not in allowed:
        raise FabricatorError(
            f"{phase} agent events require status: {', '.join(sorted(allowed))}; current status is {run['status']}"
        )


def ensure_agent_record(run: dict[str, Any], phase: str, expert_id: str) -> dict[str, Any]:
    return run.setdefault("agents", {}).setdefault(phase, {}).setdefault(expert_id, {})


def ensure_response_artifacts_exist(run_dir: Path, run: dict[str, Any], phase: str, expert_id: str) -> None:
    response_dir = "expert-responses" if phase == "expert" else f"{phase}-responses"
    missing = []
    for suffix in ("md", "json"):
        relative_path = f"{response_dir}/{expert_id}.{suffix}"
        if not (run_dir / relative_path).exists():
            missing.append(relative_path)
    if missing:
        failure = record_failure(
            run_dir,
            run,
            phase,
            expert_id,
            "missing_response",
            f"Missing response artifact(s): {', '.join(missing)}",
        )
        run["updated_at"] = failure["created_at"]
        write_json(run_dir / "run.json", run)
        raise FabricatorError(f"missing {phase} response artifact(s): {', '.join(missing)}")


def list_outstanding_agents(run: dict[str, Any]) -> list[dict[str, str]]:
    result = []
    for phase, phase_agents in run.get("agents", {}).items():
        for expert_id, agent in phase_agents.items():
            if agent["status"] not in {"completed", "failed"}:
                result.append({"phase": phase, "expert_id": expert_id, "status": agent["status"]})
    return result


def has_failure(failures: list[dict[str, Any]], phase: str, expert_id: str, kind: str) -> bool:
    return any(
        item["phase"] == phase and item["expert_id"] == expert_id and item["kind"] == kind
        for item in failures
    )


def render_summary(summary: dict[str, Any]) -> str:
    outstanding = [
        f"- `{item['phase']}` / `{item['expert_id']}`: `{item['status']}`"
        for item in summary["outstanding_agents"]
    ] or ["- None."]
    failures = [
        f"- `{item['kind']}` `{item['phase']}` / `{item['expert_id']}`: {item['message']}"
        for item in summary["failures"]
    ] or ["- None."]
    traceability = [f"- `{item}`" for item in summary["unresolved_traceability"]] or ["- None."]
    review_blockers = [
        f"- `{item['expert_id']}` `{item['verdict']}`: {', '.join(item['blockers']) or 'blocking verdict'}"
        for item in summary["review_blockers"]
    ] or ["- None."]
    files = [f"- `{item}`" for item in summary["files_to_read"]] or ["- None."]
    return "\n".join(
        [
            "# Fabricator Run Summary",
            "",
            f"Run ID: `{summary['run_id']}`",
            f"Status: `{summary['status']}`",
            f"Phase: `{summary['phase']}`",
            f"Next action: {summary['next_action']}",
            f"Next command: `{summary['next_command']}`" if summary["next_command"] else "Next command: Arbiter action required.",
            "",
            "## Outstanding Agents",
            "",
            *outstanding,
            "",
            "## Failures",
            "",
            *failures,
            "",
            "## Unresolved Traceability",
            "",
            *traceability,
            "",
            "## Review Blockers",
            "",
            *review_blockers,
            "",
            "## Files To Read",
            "",
            *files,
            "",
        ]
    )


def deadline_minutes(run: dict[str, Any], phase: str) -> int:
    return int(run.get("agent_deadline_minutes", {}).get(phase, PHASE_DEADLINE_MINUTES[phase]))


def add_minutes(timestamp: str, minutes: int) -> str:
    return (parse_time(timestamp) + dt.timedelta(minutes=minutes)).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def parse_time(timestamp: str) -> dt.datetime:
    return dt.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
