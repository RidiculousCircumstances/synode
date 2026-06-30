from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def render_prompts_for_run(
    run_dir: Path,
    run: dict[str, Any],
    experts: dict[str, dict[str, Any]],
    prompts_dir: Path,
    stance_packs_dir: Path,
) -> None:
    prompt_dir = run_dir / "expert-prompts"
    response_dir = run_dir / "expert-responses"
    prompt_dir.mkdir(parents=True, exist_ok=True)
    response_dir.mkdir(parents=True, exist_ok=True)
    intake = (run_dir / "intake.md").read_text(encoding="utf-8")
    navigation_evidence = (run_dir / "navigation-evidence.md").read_text(encoding="utf-8")
    resource_policy = run.get("resource_policy", {})
    for expert_id in run["selected_experts"]:
        prompt_body = (prompts_dir / f"{expert_id}.md").read_text(encoding="utf-8")
        stance_body = (stance_packs_dir / f"{expert_id}.md").read_text(encoding="utf-8")
        expert = experts[expert_id]
        output_path = f"expert-responses/{expert_id}.md"
        output_json_path = f"expert-responses/{expert_id}.json"
        rendered = "\n".join(
            [
                f"# Fabricator Prompt: {expert['name']}",
                "",
                prompt_body.strip(),
                "",
                "## Persona Stance Pack",
                "",
                stance_body.strip(),
                "",
                "## Run Contract",
                "",
                f"- Run ID: `{run['id']}`",
                f"- Mode: `{run['mode']}`",
                f"- Profile: `{run['profile']}`",
                f"- Expert selection source: `{run.get('expert_selection_source', 'profile')}`",
                f"- Required markdown output: `{output_path}`",
                f"- Required JSON sidecar: `{output_json_path}`",
                "- Experts are read-only advisors unless this prompt explicitly says otherwise.",
                f"- Resource guard: max active agents `{resource_policy.get('max_active_agents', 2)}`; minimum available memory `{resource_policy.get('min_available_memory_mb', 4096)} MB`.",
                f"- {resource_policy.get('heavy_command_policy', 'Do not run expensive local commands unless the Arbiter explicitly requests them.')}",
                "- Do not commit, push, open PR, open MR, or invoke GitHub write tools.",
                "- Keep the response concise and use the Fabricator expert-response shape.",
                "- The JSON sidecar must match `docs/fabricator/response-schema.md` with phase `expert`.",
                "",
                "## Task Intake",
                "",
                intake.strip(),
                "",
                "## Navigation Evidence",
                "",
                navigation_evidence.strip(),
                "",
            ]
        )
        (prompt_dir / f"{expert_id}.md").write_text(rendered, encoding="utf-8")
        response_path = response_dir / f"{expert_id}.md"
        if not response_path.exists():
            response_path.write_text(
                f"# Expert Response\n\nExpert: {expert['name']}\nTask: {run['goal']}\n\n## Recommendation\n\n\n",
                encoding="utf-8",
            )


def render_challenge_prompt(
    *,
    expert_id: str,
    expert: dict[str, Any],
    run: dict[str, Any],
    intake: str,
    navigation_evidence: str,
    challenge_brief: str,
    reason: str,
    round_number: int,
    prompts_dir: Path,
    stance_packs_dir: Path,
) -> str:
    prompt_body = (prompts_dir / f"{expert_id}.md").read_text(encoding="utf-8")
    stance_body = (stance_packs_dir / f"{expert_id}.md").read_text(encoding="utf-8")
    output_path = f"challenge-responses/{expert_id}.md"
    output_json_path = f"challenge-responses/{expert_id}.json"
    resource_policy = run.get("resource_policy", {})
    return "\n".join(
        [
            f"# Fabricator Challenge Prompt: {expert['name']}",
            "",
            prompt_body.strip(),
            "",
            "## Persona Stance Pack",
            "",
            stance_body.strip(),
            "",
            "## Run Contract",
            "",
            f"- Run ID: `{run['id']}`",
            f"- Mode: `{run['mode']}`",
            f"- Profile: `{run['profile']}`",
            f"- Required markdown output: `{output_path}`",
            f"- Required JSON sidecar: `{output_json_path}`",
            "- Experts are read-only critics in this round.",
            f"- Resource guard: max active agents `{resource_policy.get('max_active_agents', 2)}`; minimum available memory `{resource_policy.get('min_available_memory_mb', 4096)} MB`.",
            f"- {resource_policy.get('heavy_command_policy', 'Do not run expensive local commands unless the Arbiter explicitly requests them.')}",
            "- Do not commit, push, open PR, open MR, or invoke GitHub write tools.",
            "",
            "## Challenge Contract",
            "",
            f"- Challenge policy: `{run['challenge_policy']}`",
            f"- Challenge round: `{round_number}`",
            f"- Challenge reason: {reason}",
            "- Critique recommendations, assumptions, implementation boundaries, and verification plans.",
            "- Mark each objection as blocking, advisory, or accepted risk.",
            "- Do not restart task intake or propose extra scope without decision impact.",
            "- Do not open a second implementation path.",
            "- The JSON sidecar must match `docs/fabricator/response-schema.md` with phase `challenge`.",
            "",
            "## Task Intake",
            "",
            intake.strip(),
            "",
            "## Navigation Evidence",
            "",
            navigation_evidence.strip(),
            "",
            "## Challenge Brief",
            "",
            challenge_brief.strip(),
            "",
        ]
    )


def render_challenge_response_placeholder(expert: dict[str, Any], run: dict[str, Any]) -> str:
    return "\n".join(
        [
            "# Challenge Response",
            "",
            f"Expert: {expert['name']}",
            f"Task: {run['goal']}",
            "Brief reviewed: challenge-brief.md",
            "",
            "## Position",
            "",
            "- Support as written:",
            "- Support with changes:",
            "- Block:",
            "",
            "## Objections",
            "",
            "- Blocking:",
            "- Advisory:",
            "",
            "## Arbiter Decision Impact",
            "",
            "- Must change:",
            "- May change:",
            "- No change:",
            "",
        ]
    )


def write_intake(path: Path, run: dict[str, Any]) -> None:
    paths = "\n".join(f"- `{item}`" for item in run["paths"]) or "- UNCONFIRMED"
    docs = "\n".join(f"- `{item}`" for item in run["required_docs"])
    verification = "\n".join(f"- `{item}`" for item in run["verification"])
    navigation_required = str(bool(run.get("navigation_evidence_required"))).lower()
    content = f"""# Fabricator Task Intake

Run ID: `{run['id']}`
Mode: `{run['mode']}`
Profile: `{run['profile']}`
Navigation evidence required: `{navigation_required}`

## Goal

{run['goal']}

## Success Criteria

- Arbiter decision is explicit.
- Plan+Patch mode leaves working-tree changes for human review.
- Fabricator does not commit, push, open PR, or open MR.

## Affected Areas

{paths}

## Required Docs

{docs}

## Risk And Verification

- Risk profile: `{run['risk_profile']}`
- Challenge policy: `{run['challenge_policy']}`
- Challenge required at start: `{run['challenge_required']}`
{verification}

## Navigation Evidence

- Markdown: `navigation-evidence.md`
- JSON: `navigation-evidence.json`
- Required before Fabricator council dispatch: `{navigation_required}`

## Open Questions

- None recorded at run creation.
"""
    path.write_text(content, encoding="utf-8")


def write_navigation_evidence(run_dir: Path, run: dict[str, Any]) -> None:
    required = bool(run.get("navigation_evidence_required"))
    markdown = f"""# Fabricator Navigation Evidence

Run ID: `{run['id']}`
Required: `{str(required).lower()}`

Use this artifact to record project navigation before the Fabricator council is dispatched
and keep it current before the Arbiter decision barrier.

## Required When

- Fabricator profile marks navigation evidence as required.
- The task affects backend, runtime, auth/security, public contracts, UI flows,
  migrations, developer tooling, or another nontrivial implementation surface.

## Expected Evidence

- MCP context/navigation calls used, such as `context_for_change`,
  `find_contracts`, `verification_plan`, or `code_impact`.
- Codebase Memory or graph navigation when indexed and useful.
- Live-state MCP tools only when the task depends on live local state.
- Fallback commands when an MCP server or graph index is unavailable.
- Concrete findings that changed or confirmed the implementation boundary,
  expert prompts, verification, or decision.

## MCP Tools Used

- TODO

## Fallback Commands

- TODO

## Not Used And Why

- TODO

## Findings

- TODO
"""
    payload = {
        "schema_version": 1,
        "status": "pending",
        "required": required,
        "mcp_tools_used": [],
        "fallback_commands": [],
        "not_used": [],
        "findings": [],
        "notes": [],
    }
    (run_dir / "navigation-evidence.md").write_text(markdown, encoding="utf-8")
    write_json(run_dir / "navigation-evidence.json", payload)


def write_selection(
    path: Path,
    run: dict[str, Any],
    profile: dict[str, Any],
    experts: dict[str, dict[str, Any]],
) -> None:
    selected = "\n".join(
        f"- `{expert_id}`: {experts[expert_id]['purpose']}" for expert_id in run["selected_experts"]
    )
    override_reason = run.get("expert_override_reason") or "N/A"
    content = f"""# Fabricator Expert Selection

Run ID: `{run['id']}`
Profile: `{profile['id']}`
Expert selection source: `{run.get('expert_selection_source', 'profile')}`
Expert override reason: {override_reason}
Max optional experts: `{run.get('max_optional_experts', 'N/A')}`
Navigation evidence required: `{str(bool(run.get('navigation_evidence_required'))).lower()}`

## Why This Profile

{profile['description']}

## Selected Experts

{selected}

## Communication Model

- The Arbiter is the hub.
- The selected agent group is the Fabricator council.
- Experts in the Fabricator council answer independently.
- Challenge prompts are Arbiter-mediated and limited to selected non-Arbiter
  experts.
- The Arbiter synthesizes disagreements.
- Exactly one implementation path may edit files in `plan-patch` mode.
- The human developer makes the final acceptance decision.
"""
    path.write_text(content, encoding="utf-8")


def copy_template(template_name: str, destination: Path, run: dict[str, Any], templates_dir: Path) -> None:
    template = (templates_dir / template_name).read_text(encoding="utf-8")
    header = f"Run ID: `{run['id']}`\nMode: `{run['mode']}`\nProfile: `{run['profile']}`\n\n"
    destination.write_text(f"{header}{template}", encoding="utf-8")


def write_final_summary(path: Path, run: dict[str, Any]) -> None:
    result = run.get("result", "pending")
    traceability = run.get("decision_traceability", {})
    unresolved_traceability = traceability.get("unresolved", [])
    unreferenced_traceability = traceability.get("unreferenced", [])
    failures = run.get("failures", [])
    review_blockers = run.get("review_blockers", [])
    navigation = run.get("navigation_evidence", {})
    synthesis = run.get("synthesis", {})

    def render_list(items: list[Any]) -> str:
        return "\n".join(f"- `{item}`" for item in items) if items else "- None."

    def render_failures(items: list[dict[str, Any]]) -> str:
        if not items:
            return "- None."
        return "\n".join(
            f"- `{item['kind']}` `{item['phase']}` / `{item['expert_id']}`: {item['message']}"
            for item in items
        )

    def render_review_blockers(items: list[dict[str, Any]]) -> str:
        if not items:
            return "- None."
        return "\n".join(
            f"- `{item['expert_id']}` `{item['verdict']}`: {', '.join(item['blockers']) or 'blocking verdict'}"
            for item in items
        )

    content = f"""# Fabricator Final Summary

Run ID: `{run['id']}`
Status: `{run['status']}`
Result: `{result}`

## Goal

{run['goal']}

## Gates

- Decision traceability items: `{traceability.get('items', 0)}`
- Finding clusters: `{synthesis.get('finding_clusters', 0)}`
- Unresolved traceability:
{render_list(unresolved_traceability)}
- Unreferenced traceability:
{render_list(unreferenced_traceability)}
- Review blockers:
{render_review_blockers(review_blockers)}
- Failures:
{render_failures(failures)}
- Navigation evidence: `{navigation.get('status', 'unknown')}`; ready: `{navigation.get('ready', False)}`

## Human Gate

- Review the Arbiter decision, implementation diff, review report, and verification results.
- Fabricator did not commit, push, open PR, or open MR.
- Human developer decides merge, revision, or stop.
"""
    path.write_text(content, encoding="utf-8")


def create_run_dirs(run_dir: Path) -> None:
    for name in (
        "expert-prompts",
        "expert-responses",
        "challenge-prompts",
        "challenge-responses",
        "review-prompts",
        "review-responses",
        "dispatch",
        "failures",
        "evidence",
    ):
        (run_dir / name).mkdir(parents=True, exist_ok=False)


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temp_path, path)
