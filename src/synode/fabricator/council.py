from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from synode.fabricator import lifecycle, resource_guard
from synode.fabricator import synthesis as synthesis_module
from synode.fabricator.common import FabricatorError
from synode.fabricator.rendering import write_json

VALID_VERDICTS = {"proceed", "revise", "block"}
VALID_CONFIDENCE = {"high", "medium", "low"}
VALID_RESPONSE_PHASES = {"expert", "challenge", "review"}
VALID_TRACE_RESOLUTIONS = {"accepted", "rejected", "human-decision"}
TRACEABILITY_PHASES = ("expert", "challenge")
RESPONSE_REQUIRED_FIELDS = {
    "expert_id",
    "phase",
    "verdict",
    "confidence",
    "blockers",
    "advisory_findings",
    "required_constraints",
    "verification_implications",
    "challenged_recommendations",
    "decision_impact",
}
RESPONSE_LIST_FIELDS = {
    "blockers",
    "advisory_findings",
    "required_constraints",
    "verification_implications",
    "challenged_recommendations",
}
UNCERTAINTY_TERMS = (
    "unclear",
    "unknown",
    "source of truth",
    "ownership",
    "fencing",
    "retention",
    "security",
    "migration",
    "verification",
)


def dispatch_expert_agents(run_dir: Path, run: dict[str, Any], experts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    expert_ids = list(run["optional_experts"])
    if not expert_ids:
        raise FabricatorError("no optional experts selected for dispatch")
    dispatch = build_dispatch_plan(
        run=run,
        experts=experts,
        phase="expert",
        expert_ids=expert_ids,
        prompt_dir="expert-prompts",
        response_dir="expert-responses",
        reason="Initial expert analysis.",
    )
    write_dispatch(run_dir, "expert-agents", dispatch)
    update_run_dispatch(run_dir, run, "expert", dispatch, "expert-dispatch-ready")
    return {"ok": True, "run_id": run["id"], "phase": "expert", "agents": len(expert_ids)}


def validate_phase_responses(run_dir: Path, run: dict[str, Any], phase: str) -> dict[str, Any]:
    expected_ids = expected_response_ids(run, phase)
    if not expected_ids:
        raise FabricatorError(f"no expected {phase} responses")
    response_dir = response_directory(phase)
    responses = []
    for expert_id in expected_ids:
        markdown_path = run_dir / response_dir / f"{expert_id}.md"
        json_path = run_dir / response_dir / f"{expert_id}.json"
        if not markdown_path.exists():
            lifecycle.record_failure(
                run_dir,
                run,
                phase,
                expert_id,
                "missing_response",
                f"Missing markdown response: {markdown_path.relative_to(run_dir)}",
            )
            write_json(run_dir / "run.json", run)
            raise FabricatorError(f"missing {phase} markdown response: {markdown_path.relative_to(run_dir)}")
        if not json_path.exists():
            lifecycle.record_failure(
                run_dir,
                run,
                phase,
                expert_id,
                "missing_response",
                f"Missing JSON response: {json_path.relative_to(run_dir)}",
            )
            write_json(run_dir / "run.json", run)
            raise FabricatorError(f"missing {phase} JSON response: {json_path.relative_to(run_dir)}")
        try:
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            validate_response_payload(payload, expected_expert_id=expert_id, expected_phase=phase)
        except (json.JSONDecodeError, FabricatorError) as exc:
            lifecycle.record_failure(run_dir, run, phase, expert_id, "invalid_response", str(exc))
            write_json(run_dir / "run.json", run)
            raise
        lifecycle.mark_agent_completed(run_dir, run, phase=phase, expert_id=expert_id)
        responses.append(payload)
    if phase == "review":
        run["review_blockers"] = review_blockers(responses)
    run["status"] = f"{phase}-responses-collected"
    run["updated_at"] = utc_now()
    lifecycle.after_status_change(run)
    write_json(run_dir / "run.json", run)
    lifecycle.write_lifecycle(run_dir, run)
    return {"ok": True, "run_id": run["id"], "phase": phase, "responses": len(responses)}


def trace_decision(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    responses = traceability_response_payloads(run_dir, run)
    items = build_traceability_items(run_dir, run, responses=responses)
    cluster_payload = synthesis_module.build_finding_cluster_payload(run, responses, include_advisory=False)
    unresolved = [item for item in items if item["resolution"] not in VALID_TRACE_RESOLUTIONS]
    unreferenced = traceability_unreferenced_items(run_dir, items, cluster_payload["clusters"])
    payload = {
        "run_id": run["id"],
        "created_at": utc_now(),
        "items": items,
        "clusters": cluster_payload["clusters"],
        "unresolved": [item["id"] for item in unresolved],
        "unreferenced": [item["id"] for item in unreferenced],
    }
    write_json(run_dir / "decision-traceability.json", payload)
    (run_dir / "decision-traceability.md").write_text(
        synthesis_module.render_traceability_markdown(payload),
        encoding="utf-8",
    )
    run["decision_traceability"] = {
        "items": len(items),
        "unresolved": payload["unresolved"],
        "unreferenced": payload["unreferenced"],
        "updated_at": payload["created_at"],
    }
    run["updated_at"] = payload["created_at"]
    write_json(run_dir / "run.json", run)
    lifecycle.write_lifecycle(run_dir, run)
    return {"ok": True, "run_id": run["id"], **run["decision_traceability"]}


def ensure_decision_traceability_ready(run_dir: Path, run: dict[str, Any]) -> None:
    result = trace_decision(run_dir, run)
    unresolved = result["unresolved"]
    unreferenced = result["unreferenced"]
    problems = []
    if unresolved:
        problems.append(f"unresolved traceability items: {', '.join(unresolved)}")
    if unreferenced:
        problems.append(f"traceability items not referenced in Arbiter artifacts: {', '.join(unreferenced)}")
    if problems:
        raise FabricatorError("; ".join(problems))


def write_smoke_responses(run_dir: Path, run: dict[str, Any], phase: str = "expert") -> None:
    response_dir = run_dir / response_directory(phase)
    for expert_id in expected_response_ids(run, phase):
        (response_dir / f"{expert_id}.md").write_text("# Expert Response\n\n## Recommendation\n\nProceed.\n", encoding="utf-8")
        write_json(
            response_dir / f"{expert_id}.json",
            {
                "expert_id": expert_id,
                "phase": phase,
                "verdict": "proceed",
                "confidence": "medium",
                "blockers": [],
                "advisory_findings": [],
                "required_constraints": [],
                "verification_implications": ["Smoke response only."],
                "challenged_recommendations": [],
                "decision_impact": "No decision change required for smoke.",
            },
        )


def synthesize_run(run_dir: Path, run: dict[str, Any]) -> dict[str, Any]:
    response_dir = response_directory("expert")
    responses = []
    for expert_id in run["optional_experts"]:
        json_path = run_dir / response_dir / f"{expert_id}.json"
        if not json_path.exists():
            raise FabricatorError(f"missing expert JSON response: {json_path.relative_to(run_dir)}")
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        validate_response_payload(payload, expected_expert_id=expert_id, expected_phase="expert")
        responses.append(payload)

    synthesis = build_synthesis(run, responses)
    cluster_payload = {
        "schema_version": 1,
        "run_id": run["id"],
        "created_at": synthesis["created_at"],
        "clusters": synthesis["finding_clusters"],
        "source_items": synthesis["source_items"],
    }
    write_json(run_dir / "arbiter-synthesis.json", synthesis)
    write_json(run_dir / "finding-clusters.json", cluster_payload)
    (run_dir / "arbiter-synthesis.md").write_text(
        synthesis_module.render_synthesis_markdown(synthesis),
        encoding="utf-8",
    )
    (run_dir / "finding-clusters.md").write_text(
        synthesis_module.render_finding_clusters_markdown(cluster_payload),
        encoding="utf-8",
    )
    (run_dir / "decision-brief.md").write_text(
        synthesis_module.render_decision_brief_markdown(run, synthesis),
        encoding="utf-8",
    )
    run["synthesis"] = {
        "challenge_required": synthesis["challenge_required"],
        "challenge_reasons": synthesis["challenge_reasons"],
        "challenge_candidates": synthesis["challenge_candidates"],
        "finding_clusters": len(synthesis["finding_clusters"]),
        "decision_brief": "decision-brief.md",
    }
    run["challenge_required"] = bool(synthesis["challenge_required"])
    run["status"] = "synthesis-ready"
    run["updated_at"] = utc_now()
    lifecycle.after_status_change(run)
    write_json(run_dir / "run.json", run)
    lifecycle.write_lifecycle(run_dir, run)
    return {
        "ok": True,
        "run_id": run["id"],
        "challenge_required": synthesis["challenge_required"],
        "challenge_candidates": synthesis["challenge_candidates"],
        "finding_clusters": len(synthesis["finding_clusters"]),
    }


def select_challenge_experts(run: dict[str, Any]) -> list[str]:
    synthesis = run.get("synthesis")
    if not isinstance(synthesis, dict):
        raise FabricatorError("synthesis is required before challenge dispatch")
    if not synthesis.get("challenge_required"):
        raise FabricatorError("challenge is not required by synthesis")
    candidates = [item for item in synthesis.get("challenge_candidates", []) if item in run["optional_experts"]]
    if not candidates:
        candidates = list(run["optional_experts"])
    return candidates[: int(run["max_challenge_experts"])]


def dispatch_challenge_agents(
    run_dir: Path,
    run: dict[str, Any],
    experts: dict[str, dict[str, Any]],
    expert_ids: list[str],
) -> dict[str, Any]:
    dispatch = build_dispatch_plan(
        run=run,
        experts=experts,
        phase="challenge",
        expert_ids=expert_ids,
        prompt_dir="challenge-prompts",
        response_dir="challenge-responses",
        reason="Challenge Arbiter synthesis before final decision.",
    )
    write_dispatch(run_dir, "challenge-agents", dispatch)
    update_run_dispatch(run_dir, run, "challenge", dispatch, "challenge-dispatch-ready")
    return {"ok": True, "run_id": run["id"], "phase": "challenge", "agents": len(expert_ids)}


def dispatch_review_agent(run_dir: Path, run: dict[str, Any], experts: dict[str, dict[str, Any]]) -> dict[str, Any]:
    reviewer_id = choose_reviewer(run)
    prompt_dir = run_dir / "review-prompts"
    response_dir = run_dir / "review-responses"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    response_dir.mkdir(parents=True, exist_ok=True)
    (prompt_dir / f"{reviewer_id}.md").write_text(render_review_prompt(run, experts[reviewer_id]), encoding="utf-8")
    dispatch = build_dispatch_plan(
        run=run,
        experts=experts,
        phase="review",
        expert_ids=[reviewer_id],
        prompt_dir="review-prompts",
        response_dir="review-responses",
        reason="Targeted review after implementation evidence exists.",
    )
    write_dispatch(run_dir, "review-agent", dispatch)
    update_run_dispatch(run_dir, run, "review", dispatch, "review-dispatch-ready")
    return {"ok": True, "run_id": run["id"], "phase": "review", "reviewer": reviewer_id}


def validate_response_payload(payload: dict[str, Any], *, expected_expert_id: str, expected_phase: str) -> None:
    if not isinstance(payload, dict):
        raise FabricatorError("response JSON must be an object")
    missing = sorted(RESPONSE_REQUIRED_FIELDS - set(payload))
    if missing:
        raise FabricatorError(f"response JSON is missing fields: {', '.join(missing)}")
    if payload["expert_id"] != expected_expert_id:
        raise FabricatorError(f"response expert_id must be {expected_expert_id}")
    if payload["phase"] != expected_phase or payload["phase"] not in VALID_RESPONSE_PHASES:
        raise FabricatorError(f"response phase must be {expected_phase}")
    if payload["verdict"] not in VALID_VERDICTS:
        raise FabricatorError(f"response verdict must be one of: {', '.join(sorted(VALID_VERDICTS))}")
    if payload["confidence"] not in VALID_CONFIDENCE:
        raise FabricatorError(f"response confidence must be one of: {', '.join(sorted(VALID_CONFIDENCE))}")
    for field in RESPONSE_LIST_FIELDS:
        value = payload[field]
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise FabricatorError(f"response {field} must be a list of strings")
    if not isinstance(payload["decision_impact"], str):
        raise FabricatorError("response decision_impact must be a string")


def review_blockers(responses: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blockers = []
    for response in responses:
        if response["verdict"] != "block" and not response["blockers"]:
            continue
        blockers.append(
            {
                "expert_id": response["expert_id"],
                "verdict": response["verdict"],
                "blockers": list(response["blockers"]),
            }
        )
    return blockers


def build_synthesis(run: dict[str, Any], responses: list[dict[str, Any]]) -> dict[str, Any]:
    verdicts = {item["expert_id"]: item["verdict"] for item in responses}
    blockers = [item["expert_id"] for item in responses if item["verdict"] == "block" or item["blockers"]]
    challenged = [item["expert_id"] for item in responses if item["challenged_recommendations"]]
    uncertainty = [item["expert_id"] for item in responses if response_mentions_uncertainty(item)]
    cluster_payload = synthesis_module.build_finding_cluster_payload(run, responses, include_advisory=True)
    reasons = []
    if run["challenge_policy"] == "auto":
        reasons.append("challenge_policy_auto")
    if blockers:
        reasons.append("blocker_or_blocking_verdict")
    if "block" in verdicts.values() and "proceed" in verdicts.values():
        reasons.append("conflicting_block_and_proceed_verdicts")
    if challenged:
        reasons.append("challenged_recommendations_present")
    if uncertainty:
        reasons.append("ownership_source_of_truth_or_verification_uncertainty")

    challenge_required = run["challenge_policy"] == "auto" or (
        run["challenge_policy"] == "conditional" and bool(reasons)
    )
    candidates = dedupe([*blockers, *challenged, *uncertainty])
    if challenge_required and not candidates:
        candidates = list(run["optional_experts"])

    return {
        "run_id": run["id"],
        "profile": run["profile"],
        "challenge_policy": run["challenge_policy"],
        "verdicts": verdicts,
        "blocker_experts": blockers,
        "challenged_recommendation_experts": challenged,
        "uncertainty_experts": uncertainty,
        "challenge_required": challenge_required,
        "challenge_reasons": reasons,
        "challenge_candidates": candidates[: int(run["max_challenge_experts"])],
        "finding_clusters": cluster_payload["clusters"],
        "source_items": cluster_payload["source_items"],
        "created_at": utc_now(),
    }


def build_dispatch_plan(
    *,
    run: dict[str, Any],
    experts: dict[str, dict[str, Any]],
    phase: str,
    expert_ids: list[str],
    prompt_dir: str,
    response_dir: str,
    reason: str,
) -> dict[str, Any]:
    agents = []
    for expert_id in expert_ids:
        if expert_id not in experts:
            raise FabricatorError(f"unknown dispatch expert: {expert_id}")
        agents.append(
            {
                "expert_id": expert_id,
                "agent_type": "explorer",
                "fork_context": False,
                "prompt_path": f"{prompt_dir}/{expert_id}.md",
                "response_markdown_path": f"{response_dir}/{expert_id}.md",
                "response_json_path": f"{response_dir}/{expert_id}.json",
                "read_only": True,
                "forbidden_actions": run["git_policy"]["forbidden_actions"],
                "message": (
                    f"Read `{prompt_dir}/{expert_id}.md`, perform read-only {phase} analysis, "
                    f"then return markdown and JSON matching `docs/fabricator/response-schema.md`."
                ),
            }
        )
    return {
        "run_id": run["id"],
        "phase": phase,
        "reason": reason,
        "created_at": utc_now(),
        "resource_policy": resource_guard.effective_policy(run),
        "agents": agents,
    }


def write_dispatch(run_dir: Path, name: str, dispatch: dict[str, Any]) -> None:
    dispatch_dir = run_dir / "dispatch"
    dispatch_dir.mkdir(parents=True, exist_ok=True)
    write_json(dispatch_dir / f"{name}.json", dispatch)
    (dispatch_dir / f"{name}.md").write_text(render_dispatch_markdown(dispatch), encoding="utf-8")


def update_run_dispatch(run_dir: Path, run: dict[str, Any], key: str, dispatch: dict[str, Any], status: str) -> None:
    expert_ids = [agent["expert_id"] for agent in dispatch["agents"]]
    run.setdefault("dispatch", {})[key] = {
        "phase": dispatch["phase"],
        "agents": expert_ids,
        "created_at": dispatch["created_at"],
    }
    lifecycle.register_dispatch(run, dispatch["phase"], expert_ids, dispatch["created_at"])
    run["status"] = status
    run["updated_at"] = utc_now()
    lifecycle.after_status_change(run)
    write_json(run_dir / "run.json", run)
    lifecycle.write_lifecycle(run_dir, run)


def expected_response_ids(run: dict[str, Any], phase: str) -> list[str]:
    if phase == "expert":
        return list(run["optional_experts"])
    if phase == "challenge":
        rounds = run.get("challenge_rounds") or []
        return list(rounds[-1]["experts"]) if rounds else []
    if phase == "review":
        review = run.get("dispatch", {}).get("review", {})
        return list(review.get("agents", []))
    raise FabricatorError(f"unknown response phase: {phase}")


def build_traceability_items(
    run_dir: Path,
    run: dict[str, Any],
    *,
    responses: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    existing = existing_traceability_items(run_dir)
    items = []
    for payload in responses if responses is not None else traceability_response_payloads(run_dir, run):
        items.extend(traceability_items_for_response(payload, existing))
    return items


def traceability_response_payloads(run_dir: Path, run: dict[str, Any]) -> list[dict[str, Any]]:
    responses = []
    for phase in TRACEABILITY_PHASES:
        for expert_id in expected_response_ids(run, phase):
            json_path = run_dir / response_directory(phase) / f"{expert_id}.json"
            if not json_path.exists():
                continue
            payload = json.loads(json_path.read_text(encoding="utf-8"))
            validate_response_payload(payload, expected_expert_id=expert_id, expected_phase=phase)
            responses.append(payload)
    return responses


def existing_traceability_items(run_dir: Path) -> dict[str, dict[str, Any]]:
    path = run_dir / "decision-traceability.json"
    if not path.exists():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    return {item["id"]: item for item in payload.get("items", []) if isinstance(item, dict) and "id" in item}


def traceability_items_for_response(
    response: dict[str, Any],
    existing: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    return synthesis_module.traceability_items_for_response(response, existing)


def traceability_unreferenced_items(
    run_dir: Path,
    items: list[dict[str, Any]],
    clusters: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    resolved = [item for item in items if item["resolution"] in VALID_TRACE_RESOLUTIONS]
    if not resolved:
        return []
    text = "\n".join(
        (run_dir / name).read_text(encoding="utf-8")
        for name in ("arbiter-decision.md", "implementation-handoff.md", "design-note.md")
        if (run_dir / name).exists()
    )
    cluster_ids_by_item = synthesis_module.cluster_ids_for_source_items(clusters)
    return [
        item
        for item in resolved
        if item["id"] not in text
        and not any(cluster_id in text for cluster_id in cluster_ids_by_item.get(item["id"], []))
    ]


def response_directory(phase: str) -> str:
    if phase not in VALID_RESPONSE_PHASES:
        raise FabricatorError(f"phase must be one of: {', '.join(sorted(VALID_RESPONSE_PHASES))}")
    return f"{phase}-responses" if phase != "expert" else "expert-responses"


def response_mentions_uncertainty(response: dict[str, Any]) -> bool:
    text = " ".join(
        str(item)
        for field in (*RESPONSE_LIST_FIELDS, "decision_impact")
        for item in ([response[field]] if isinstance(response[field], str) else response[field])
    ).lower()
    return any(term in text for term in UNCERTAINTY_TERMS)


def choose_reviewer(run: dict[str, Any]) -> str:
    optional = list(run["optional_experts"])
    if "red_team_reviewer" in optional:
        return "red_team_reviewer"
    if "qa_test_strategist" in optional:
        return "qa_test_strategist"
    if optional:
        return optional[0]
    raise FabricatorError("no selected expert is available for review dispatch")


def render_review_prompt(run: dict[str, Any], expert: dict[str, Any]) -> str:
    return "\n".join(
        [
            f"# Fabricator Review Prompt: {expert['name']}",
            "",
            "## Run Contract",
            "",
            f"- Run ID: `{run['id']}`",
            f"- Mode: `{run['mode']}`",
            f"- Profile: `{run['profile']}`",
            "- Review is read-only.",
            f"- Resource guard: max active agents `{run.get('resource_policy', {}).get('max_active_agents', resource_guard.DEFAULT_MAX_ACTIVE_AGENTS)}`; "
            f"minimum available memory `{run.get('resource_policy', {}).get('min_available_memory_mb', resource_guard.DEFAULT_MIN_AVAILABLE_MB)} MB`.",
            f"- {resource_guard.HEAVY_COMMAND_POLICY}",
            "- Do not commit, push, open PR, open MR, or invoke GitHub write tools.",
            "- Check the implementation against `arbiter-decision.md` and `implementation-handoff.md`.",
            "- Return markdown plus JSON matching `docs/fabricator/response-schema.md` with phase `review`.",
            "",
        ]
    )


def render_dispatch_markdown(dispatch: dict[str, Any]) -> str:
    lines = [
        f"# Fabricator {dispatch['phase'].title()} Dispatch",
        "",
        f"Run ID: `{dispatch['run_id']}`",
        f"Reason: {dispatch['reason']}",
        "",
        "## Resource Guard",
        "",
        f"- Max active Fabricator agents: `{dispatch['resource_policy']['max_active_agents']}`",
        f"- Minimum available memory before dispatch/start: `{dispatch['resource_policy']['min_available_memory_mb']} MB`",
        f"- Batch size: start at most `{dispatch['resource_policy']['max_active_agents']}` agent(s), then wait for completion/failure before starting more.",
        f"- Heavy command policy: {dispatch['resource_policy']['heavy_command_policy']}",
        "- Run `agent-started` before each subagent; if the guard blocks, wait, complete, or fail another agent first.",
        "",
        "## Agents",
        "",
    ]
    for agent in dispatch["agents"]:
        lines.extend(
            [
                f"- `{agent['expert_id']}`",
                f"  - Agent type: `{agent['agent_type']}`",
                f"  - Prompt: `{agent['prompt_path']}`",
                f"  - Markdown response: `{agent['response_markdown_path']}`",
                f"  - JSON response: `{agent['response_json_path']}`",
                "  - Read-only: `true`",
            ]
        )
    lines.append("")
    return "\n".join(lines)


def dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
