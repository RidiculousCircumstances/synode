#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import re
import sys
from pathlib import Path
from typing import Any

try:  # Python 3.11+.
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - exercised on Python 3.10 only.
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:  # pragma: no cover - depends on local Python environment.
        tomllib = None  # type: ignore[assignment]

try:
    from . import (
        council,
        expert_selection,
        lifecycle,
        navigation,
        prompt_validation,
        resource_guard,
    )
    from . import profiles as profile_selection
    from .common import FabricatorError
    from .rendering import (
        copy_template,
        create_run_dirs,
        render_challenge_prompt,
        render_challenge_response_placeholder,
        render_prompts_for_run,
        write_final_summary,
        write_intake,
        write_json,
        write_navigation_evidence,
        write_selection,
    )
except ImportError:  # pragma: no cover - supports direct script execution.
    sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
    from synode.fabricator import (
        council,
        expert_selection,
        lifecycle,
        navigation,
        prompt_validation,
        resource_guard,
    )
    from synode.fabricator import profiles as profile_selection
    from synode.fabricator.common import FabricatorError
    from synode.fabricator.rendering import (
        copy_template,
        create_run_dirs,
        render_challenge_prompt,
        render_challenge_response_placeholder,
        render_prompts_for_run,
        write_final_summary,
        write_intake,
        write_json,
        write_navigation_evidence,
        write_selection,
    )


REPO_ROOT = Path(__file__).resolve().parents[3]
FABRICATOR_DOCS = REPO_ROOT / "docs" / "fabricator"
EXPERTS_PATH = FABRICATOR_DOCS / "experts.toml"
ROUTING_PATH = FABRICATOR_DOCS / "routing.toml"
PROMPTS_DIR = FABRICATOR_DOCS / "prompts"
STANCE_PACKS_DIR = FABRICATOR_DOCS / "stance-packs"
TEMPLATES_DIR = FABRICATOR_DOCS / "templates"
DEFAULT_RUNS_DIR = REPO_ROOT / "var" / "fabricator" / "runs"
VALID_MODES = {"plan-patch", "plan-only", "review-only"}
VALID_RESULTS = {"ready-for-human-review", "blocked", "cancelled"}
VALID_CHALLENGE_POLICIES = {"manual", "conditional", "auto"}
EXPERT_REQUIRED_FIELDS = {
    "id",
    "name",
    "purpose",
    "activate_when",
    "do_not_activate_when",
    "inputs_required",
    "output_focus",
    "default_review_role",
}
PROFILE_REQUIRED_FIELDS = {
    "id",
    "description",
    "risk_profile",
    "experts",
    "required_docs",
    "verification",
    "navigation_required",
    "challenge_policy",
    "challenge_triggers",
    "max_challenge_experts",
}
REQUIRED_TEMPLATES = {
    "task-intake.md",
    "expert-response.md",
    "challenge-brief.md",
    "challenge-response.md",
    "arbiter-decision.md",
    "implementation-handoff.md",
    "review-report.md",
    "design-note.md",
}
REQUIRED_PROMPT_SECTIONS = prompt_validation.REQUIRED_PROMPT_SECTIONS
REQUIRED_STANCE_SECTIONS = prompt_validation.REQUIRED_STANCE_SECTIONS


def load_toml(path: Path) -> dict[str, Any]:
    if tomllib is None:
        raise FabricatorError("Python 3.11+ or the tomli package is required to read Fabricator TOML files")
    if not path.exists():
        raise FabricatorError(f"missing TOML file: {path.relative_to(REPO_ROOT)}")
    return tomllib.loads(path.read_text(encoding="utf-8"))


def validate_all() -> dict[str, Any]:
    experts_data = load_toml(EXPERTS_PATH)
    routing_data = load_toml(ROUTING_PATH)
    experts = validate_experts(experts_data)
    profiles = validate_routing(routing_data, set(experts))
    validate_prompts(set(experts))
    validate_stance_packs(set(experts))
    validate_templates()
    return {
        "ok": True,
        "experts": len(experts),
        "profiles": len(profiles),
        "prompts": len(experts),
        "persona_sections": len(experts) * len(REQUIRED_PROMPT_SECTIONS),
        "stance_packs": len(experts),
        "stance_sections": len(experts) * len(REQUIRED_STANCE_SECTIONS),
        "templates": len(REQUIRED_TEMPLATES),
    }


def validate_experts(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    if data.get("schema_version") != 1:
        raise FabricatorError("experts.toml schema_version must be 1")
    defaults = require_table(data, "defaults")
    experts = require_list(data, "experts")
    ids: set[str] = set()
    result: dict[str, dict[str, Any]] = {}
    for expert in experts:
        if not isinstance(expert, dict):
            raise FabricatorError("each expert must be a table")
        missing = sorted(EXPERT_REQUIRED_FIELDS - set(expert))
        if missing:
            raise FabricatorError(f"expert is missing fields: {', '.join(missing)}")
        expert_id = require_id(expert["id"], "expert id")
        if expert_id in ids:
            raise FabricatorError(f"duplicate expert id: {expert_id}")
        ids.add(expert_id)
        result[expert_id] = expert
    mandatory = str(defaults.get("mandatory_expert") or "")
    if mandatory != expert_selection.MANDATORY_EXPERT_ID or mandatory not in result:
        raise FabricatorError(f"defaults.mandatory_expert must be {expert_selection.MANDATORY_EXPERT_ID}")
    if result[expert_selection.MANDATORY_EXPERT_ID].get("mandatory") is not True:
        raise FabricatorError(f"{expert_selection.MANDATORY_EXPERT_ID} must set mandatory = true")
    max_experts = defaults.get("default_max_optional_experts")
    if not isinstance(max_experts, int) or max_experts < 1:
        raise FabricatorError("defaults.default_max_optional_experts must be a positive integer")
    return result


def validate_routing(data: dict[str, Any], expert_ids: set[str]) -> dict[str, dict[str, Any]]:
    if data.get("schema_version") != 1:
        raise FabricatorError("routing.toml schema_version must be 1")
    defaults = require_table(data, "defaults")
    forbidden = defaults.get("forbidden_git_actions")
    if not isinstance(forbidden, list) or not forbidden:
        raise FabricatorError("routing defaults must declare forbidden_git_actions")
    if not {"commit", "push", "open_pr", "open_mr"} <= {str(item) for item in forbidden}:
        raise FabricatorError("forbidden_git_actions must include commit, push, open_pr, and open_mr")
    profiles = require_list(data, "profiles")
    result: dict[str, dict[str, Any]] = {}
    for profile in profiles:
        if not isinstance(profile, dict):
            raise FabricatorError("each routing profile must be a table")
        missing = sorted(PROFILE_REQUIRED_FIELDS - set(profile))
        if missing:
            raise FabricatorError(f"routing profile is missing fields: {', '.join(missing)}")
        profile_id = require_id(profile["id"], "profile id")
        if profile_id in result:
            raise FabricatorError(f"duplicate routing profile id: {profile_id}")
        experts = require_string_list(profile, "experts")
        unknown = sorted(set(experts) - expert_ids)
        if unknown:
            raise FabricatorError(f"profile {profile_id} references unknown experts: {', '.join(unknown)}")
        max_optional = int(profile.get("max_optional_experts") or defaults["max_optional_experts"])
        if len(experts) > max_optional:
            raise FabricatorError(f"profile {profile_id} selects {len(experts)} experts, above cap {max_optional}")
        challenge_policy = str(profile.get("challenge_policy") or "")
        if challenge_policy not in VALID_CHALLENGE_POLICIES:
            raise FabricatorError(
                f"profile {profile_id} challenge_policy must be one of: "
                f"{', '.join(sorted(VALID_CHALLENGE_POLICIES))}"
            )
        require_string_list(profile, "challenge_triggers")
        if not isinstance(profile.get("navigation_required"), bool):
            raise FabricatorError(f"profile {profile_id} navigation_required must be true or false")
        max_challenge = profile.get("max_challenge_experts")
        if not isinstance(max_challenge, int) or max_challenge < 1:
            raise FabricatorError(f"profile {profile_id} max_challenge_experts must be a positive integer")
        if max_challenge > len(experts):
            raise FabricatorError(
                f"profile {profile_id} allows {max_challenge} challenge experts, "
                f"above selected optional expert count {len(experts)}"
            )
        result[profile_id] = profile
    return result


def validate_prompts(expert_ids: set[str]) -> None:
    prompt_validation.validate_section_files(
        expert_ids, PROMPTS_DIR, REQUIRED_PROMPT_SECTIONS, "prompt", prompt_validation.prompt_sections, display_path
    )


def validate_stance_packs(expert_ids: set[str]) -> None:
    prompt_validation.validate_section_files(
        expert_ids,
        STANCE_PACKS_DIR,
        REQUIRED_STANCE_SECTIONS,
        "stance pack",
        prompt_validation.stance_sections,
        display_path,
    )


def validate_templates() -> None:
    missing = sorted(name for name in REQUIRED_TEMPLATES if not (TEMPLATES_DIR / name).exists())
    if missing:
        raise FabricatorError(f"missing templates: {', '.join(missing)}")


def start_run(
    *,
    mode: str,
    goal: str,
    paths: list[str],
    profile_id: str | None,
    run_id: str | None,
    runs_dir: Path,
    expert_override_ids: list[str] | None = None,
    expert_override_reason: str | None = None,
) -> dict[str, Any]:
    validate_all()
    experts = validate_experts(load_toml(EXPERTS_PATH))
    routing = load_toml(ROUTING_PATH)
    profiles = validate_routing(routing, set(experts))
    if mode not in VALID_MODES:
        raise FabricatorError(f"mode must be one of: {', '.join(sorted(VALID_MODES))}")
    if profile_id and profile_id not in profiles:
        raise FabricatorError(f"unknown routing profile: {profile_id}")
    selected_profile = profiles[profile_id] if profile_id else profile_selection.infer_profile(goal, paths, profiles)
    selected_experts = expert_selection.select_run_experts(
        selected_profile=selected_profile,
        experts=experts,
        expert_override_ids=expert_override_ids,
        expert_override_reason=expert_override_reason,
    )
    selected = selected_experts["selected_experts"]
    optional_experts = selected_experts["optional_experts"]
    effective_run_id = run_id or build_run_id(goal)
    run_dir = runs_dir / effective_run_id
    if run_dir.exists():
        raise FabricatorError(f"run already exists: {run_dir}")
    create_run_dirs(run_dir)
    with lifecycle.run_lock(run_dir, "start"):
        created_at = utc_now()
        run = {
            "id": effective_run_id,
            "status": "started",
            "mode": mode,
            "goal": goal,
            "paths": paths,
            "profile": selected_profile["id"],
            "risk_profile": selected_profile["risk_profile"],
            "selected_experts": selected,
            "optional_experts": optional_experts,
            "expert_selection_source": selected_experts["expert_selection_source"],
            "expert_override_reason": selected_experts["expert_override_reason"],
            "max_optional_experts": expert_selection.MAX_OPTIONAL_EXPERTS,
            "challenge_policy": selected_profile["challenge_policy"],
            "challenge_triggers": selected_profile["challenge_triggers"],
            "challenge_required": selected_profile["challenge_policy"] == "auto",
            "navigation_evidence_required": selected_profile["navigation_required"],
            "navigation_evidence": {
                "required": selected_profile["navigation_required"],
                "ready": not selected_profile["navigation_required"],
                "status": "pending",
                "path": navigation.NAVIGATION_EVIDENCE_JSON,
            },
            "max_challenge_experts": selected_experts["max_challenge_experts"],
            "challenge_rounds": [],
            "lifecycle_version": "v3",
            "dispatch": {},
            "resource_policy": resource_guard.default_policy(),
            "resource_checks": [],
            "created_at": created_at,
            "updated_at": created_at,
            "runs_dir": display_path(runs_dir),
            "git_policy": {
                "allowed_working_tree_edits": mode == "plan-patch",
                "forbidden_actions": routing["defaults"]["forbidden_git_actions"],
                "human_merge_gate": routing["defaults"]["human_merge_gate"],
            },
            "verification": selected_profile["verification"],
            "required_docs": selected_profile["required_docs"],
        }
        lifecycle.initialize_run(run)
        write_json(run_dir / "run.json", run)
        lifecycle.write_lifecycle(run_dir, run)
        write_intake(run_dir / "intake.md", run)
        write_navigation_evidence(run_dir, run)
        write_selection(run_dir / "selection.md", run, selected_profile, experts)
        render_prompts_for_run(run_dir, run, experts, PROMPTS_DIR, STANCE_PACKS_DIR)
        copy_template("challenge-brief.md", run_dir / "challenge-brief.md", run, TEMPLATES_DIR)
        copy_template("arbiter-decision.md", run_dir / "arbiter-decision.md", run, TEMPLATES_DIR)
        copy_template("implementation-handoff.md", run_dir / "implementation-handoff.md", run, TEMPLATES_DIR)
        copy_template("review-report.md", run_dir / "review-report.md", run, TEMPLATES_DIR)
        write_final_summary(run_dir / "final-summary.md", run)
    return {"ok": True, "run_id": effective_run_id, "run_dir": str(run_dir), "profile": selected_profile["id"]}


def render_prompts(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "render-prompts"):
        run = read_run(run_dir)
        experts = validate_experts(load_toml(EXPERTS_PATH))
        validate_stance_packs(set(experts))
        render_prompts_for_run(run_dir, run, experts, PROMPTS_DIR, STANCE_PACKS_DIR)
        touch_run(run_dir, run, "prompts-rendered")
        return {"ok": True, "run_id": run_id, "prompts": len(run["selected_experts"])}


def render_challenge_prompts(
    *,
    run_id: str,
    runs_dir: Path,
    expert_ids: list[str],
    reason: str,
) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "render-challenge-prompts"):
        return _render_challenge_prompts(
            run_id=run_id,
            runs_dir=runs_dir,
            expert_ids=expert_ids,
            reason=reason,
        )


def _render_challenge_prompts(
    *,
    run_id: str,
    runs_dir: Path,
    expert_ids: list[str],
    reason: str,
) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    run = read_run(run_dir)
    experts = validate_experts(load_toml(EXPERTS_PATH))
    validate_stance_packs(set(experts))
    selected = set(run["optional_experts"])
    unique_experts = dedupe_preserving_order(expert_ids)
    if not unique_experts:
        raise FabricatorError("at least one challenge expert is required")
    unknown = sorted(set(unique_experts) - set(experts))
    if unknown:
        raise FabricatorError(f"unknown challenge experts: {', '.join(unknown)}")
    not_selected = sorted(set(unique_experts) - selected)
    if not_selected:
        raise FabricatorError(f"challenge experts must be selected optional experts: {', '.join(not_selected)}")
    max_challenge = int(run["max_challenge_experts"])
    if len(unique_experts) > max_challenge:
        raise FabricatorError(
            f"challenge expert count {len(unique_experts)} exceeds run cap {max_challenge}"
        )
    challenge_brief_path = run_dir / "challenge-brief.md"
    if not challenge_brief_path.exists():
        raise FabricatorError("challenge-brief.md is required before rendering challenge prompts")

    challenge_rounds = run.setdefault("challenge_rounds", [])
    if challenge_rounds:
        raise FabricatorError("challenge round already exists; stop for Arbiter or human decision")
    round_number = len(challenge_rounds) + 1
    prompt_dir = run_dir / "challenge-prompts"
    response_dir = run_dir / "challenge-responses"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    response_dir.mkdir(parents=True, exist_ok=True)
    intake = (run_dir / "intake.md").read_text(encoding="utf-8")
    navigation_evidence = (run_dir / navigation.NAVIGATION_EVIDENCE_MARKDOWN).read_text(encoding="utf-8")
    challenge_brief = challenge_brief_path.read_text(encoding="utf-8")
    for expert_id in unique_experts:
        rendered = render_challenge_prompt(
            expert_id=expert_id,
            expert=experts[expert_id],
            run=run,
            intake=intake,
            navigation_evidence=navigation_evidence,
            challenge_brief=challenge_brief,
            reason=reason,
            round_number=round_number,
            prompts_dir=PROMPTS_DIR,
            stance_packs_dir=STANCE_PACKS_DIR,
        )
        (prompt_dir / f"{expert_id}.md").write_text(rendered, encoding="utf-8")
        response_path = response_dir / f"{expert_id}.md"
        if not response_path.exists():
            response_path.write_text(render_challenge_response_placeholder(experts[expert_id], run), encoding="utf-8")

    created_at = utc_now()
    challenge_rounds.append(
        {
            "round": round_number,
            "experts": unique_experts,
            "reason": reason,
            "brief": "challenge-brief.md",
            "created_at": created_at,
        }
    )
    run["challenge_required"] = True
    run["updated_at"] = created_at
    run["status"] = "challenge-prompts-rendered"
    write_json(run_dir / "run.json", run)
    return {
        "ok": True,
        "run_id": run_id,
        "round": round_number,
        "challenge_prompts": len(unique_experts),
        "experts": unique_experts,
    }


def dispatch_experts(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "dispatch-experts"):
        run = read_run(run_dir)
        navigation.ensure_navigation_evidence_ready(run_dir, run)
        lifecycle.ensure_command_allowed(run, "dispatch-experts")
        resource_guard.assert_can_dispatch(run, phase="expert")
        experts = validate_experts(load_toml(EXPERTS_PATH))
        render_prompts_for_run(run_dir, run, experts, PROMPTS_DIR, STANCE_PACKS_DIR)
        return council.dispatch_expert_agents(run_dir, run, experts)


def validate_responses(run_id: str, runs_dir: Path, phase: str) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, f"validate-responses:{phase}"):
        run = read_run(run_dir)
        lifecycle.ensure_command_allowed(run, f"validate-responses:{phase}")
        return council.validate_phase_responses(run_dir, run, phase)


def synthesize(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "synthesize"):
        run = read_run(run_dir)
        lifecycle.ensure_command_allowed(run, "synthesize")
        return council.synthesize_run(run_dir, run)


def dispatch_challenge(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "dispatch-challenge"):
        run = read_run(run_dir)
        lifecycle.ensure_command_allowed(run, "dispatch-challenge")
        expert_ids = council.select_challenge_experts(run)
        reason = ", ".join(run["synthesis"]["challenge_reasons"]) or "Challenge required by synthesis."
        _render_challenge_prompts(run_id=run_id, runs_dir=runs_dir, expert_ids=expert_ids, reason=reason)
        run = read_run(run_dir)
        resource_guard.assert_can_dispatch(run, phase="challenge")
        experts = validate_experts(load_toml(EXPERTS_PATH))
        return council.dispatch_challenge_agents(run_dir, run, experts, expert_ids)


def dispatch_review(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "dispatch-review"):
        run = read_run(run_dir)
        lifecycle.ensure_command_allowed(run, "dispatch-review")
        resource_guard.assert_can_dispatch(run, phase="review")
        experts = validate_experts(load_toml(EXPERTS_PATH))
        return council.dispatch_review_agent(run_dir, run, experts)


def trace_decision(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "trace-decision"):
        run = read_run(run_dir)
        return council.trace_decision(run_dir, run)


def next_step(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "next"):
        run = read_run(run_dir)
        navigation.refresh_navigation_evidence(run_dir, run)
        return lifecycle.next_action(run_dir, run)


def advance(run_id: str, runs_dir: Path) -> dict[str, Any]:
    from synode.fabricator import advance as advance_module

    return advance_module.advance(run_id, runs_dir)


def summarize(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "summary"):
        run = read_run(run_dir)
        navigation.refresh_navigation_evidence(run_dir, run)
        return lifecycle.summarize(run_dir, run)


def agent_started(run_id: str, runs_dir: Path, phase: str, expert_id: str, agent_id: str | None) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, f"agent-started:{phase}:{expert_id}"):
        run = read_run(run_dir)
        lifecycle.ensure_agent_event_allowed(run, phase)
        lifecycle.ensure_expected_agent(run, phase, expert_id)
        resource_guard.assert_can_start_agent(run, phase=phase, expert_id=expert_id)
        return lifecycle.mark_agent_started(run_dir, run, phase=phase, expert_id=expert_id, agent_id=agent_id)


def agent_completed(run_id: str, runs_dir: Path, phase: str, expert_id: str) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, f"agent-completed:{phase}:{expert_id}"):
        run = read_run(run_dir)
        return lifecycle.mark_agent_completed(run_dir, run, phase=phase, expert_id=expert_id)


def agent_failed(run_id: str, runs_dir: Path, phase: str, expert_id: str, reason: str) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, f"agent-failed:{phase}:{expert_id}"):
        run = read_run(run_dir)
        return lifecycle.mark_agent_failed(run_dir, run, phase=phase, expert_id=expert_id, reason=reason)


def agent_timeouts(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "agent-timeouts"):
        run = read_run(run_dir)
        return lifecycle.agent_timeouts(run_dir, run)


def mark_decision_ready(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "mark-decision-ready"):
        run = read_run(run_dir)
        navigation.ensure_navigation_evidence_ready(run_dir, run)
        council.ensure_decision_traceability_ready(run_dir, run)
        run = read_run(run_dir)
        return lifecycle.mark_decision_ready(run_dir, run)


def mark_implementation_ready(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "mark-implementation-ready"):
        run = read_run(run_dir)
        return lifecycle.mark_implementation_ready(run_dir, run)


def status_run(run_id: str, runs_dir: Path) -> dict[str, Any]:
    run_dir = runs_dir / run_id
    run = read_run(run_dir)
    navigation.refresh_navigation_evidence(run_dir, run)
    expected_files = [
        "intake.md",
        "selection.md",
        navigation.NAVIGATION_EVIDENCE_MARKDOWN,
        navigation.NAVIGATION_EVIDENCE_JSON,
        "challenge-brief.md",
        "arbiter-decision.md",
        "implementation-handoff.md",
        "review-report.md",
        "final-summary.md",
    ]
    return {
        "ok": True,
        "run_id": run_id,
        "status": run["status"],
        "mode": run["mode"],
        "profile": run["profile"],
        "selected_experts": run["selected_experts"],
        "challenge_policy": run.get("challenge_policy"),
        "challenge_required": run.get("challenge_required", False),
        "challenge_rounds": run.get("challenge_rounds", []),
        "lifecycle_version": run.get("lifecycle_version", "v2"),
        "state_machine_version": run.get("state_machine_version"),
        "current_phase": run.get("current_phase"),
        "allowed_next_commands": run.get("allowed_next_commands", []),
        "dispatch": run.get("dispatch", {}),
        "synthesis": run.get("synthesis", {}),
        "decision_traceability": run.get("decision_traceability", {}),
        "navigation_evidence": run.get("navigation_evidence", {}),
        "review_blockers": run.get("review_blockers", []),
        "agents": run.get("agents", {}),
        "failures": run.get("failures", []),
        "missing_files": [name for name in expected_files if not (run_dir / name).exists()],
    }


def finalize_run(run_id: str, runs_dir: Path, result: str) -> dict[str, Any]:
    if result not in VALID_RESULTS:
        raise FabricatorError(f"result must be one of: {', '.join(sorted(VALID_RESULTS))}")
    run_dir = runs_dir / run_id
    with lifecycle.run_lock(run_dir, "finalize"):
        run = read_run(run_dir)
        if result == "ready-for-human-review":
            lifecycle.ensure_command_allowed(run, "finalize")
            if run.get("review_blockers"):
                raise FabricatorError("review blockers must be resolved before finalization")
        elif run["status"] in lifecycle.TERMINAL_STATUSES:
            raise FabricatorError(f"run is terminal ({run['status']}); finalize is not allowed")
        run["result"] = result
        run["status"] = "completed" if result == "ready-for-human-review" else result
        run["updated_at"] = utc_now()
        run["final_notice"] = "Fabricator did not commit, push, open PR, or open MR."
        lifecycle.after_status_change(run)
        write_json(run_dir / "run.json", run)
        lifecycle.write_lifecycle(run_dir, run)
        write_final_summary(run_dir / "final-summary.md", run)
        return {"ok": True, "run_id": run_id, "status": run["status"], "result": result}


def read_run(run_dir: Path) -> dict[str, Any]:
    path = run_dir / "run.json"
    if not path.exists():
        raise FabricatorError(f"run not found: {run_dir}")
    return json.loads(path.read_text(encoding="utf-8"))


def touch_run(run_dir: Path, run: dict[str, Any], status: str) -> None:
    run["status"] = status
    run["updated_at"] = utc_now()
    write_json(run_dir / "run.json", run)


def require_table(data: dict[str, Any], key: str) -> dict[str, Any]:
    value = data.get(key)
    if not isinstance(value, dict):
        raise FabricatorError(f"{key} must be a table")
    return value


def require_list(data: dict[str, Any], key: str) -> list[Any]:
    value = data.get(key)
    if not isinstance(value, list):
        raise FabricatorError(f"{key} must be a list")
    return value


def require_string_list(data: dict[str, Any], key: str) -> list[str]:
    value = data.get(key)
    if not isinstance(value, list) or not all(isinstance(item, str) and item for item in value):
        raise FabricatorError(f"{key} must be a non-empty string list")
    return value


def require_id(value: Any, label: str) -> str:
    text = str(value or "")
    if not re.fullmatch(r"[a-z][a-z0-9_]*", text):
        raise FabricatorError(f"{label} must match [a-z][a-z0-9_]*")
    return text


def dedupe_preserving_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


infer_profile = profile_selection.infer_profile


def build_run_id(goal: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", goal.lower()).strip("-")[:48] or "fabricator-run"
    return f"{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{slug}"


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def display_path(path: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(REPO_ROOT.resolve()))
    except ValueError:
        return str(path)


def main(argv: list[str] | None = None) -> int:
    from synode.fabricator.cli import main as cli_main

    return cli_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
