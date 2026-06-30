from __future__ import annotations

import json
from pathlib import Path

import pytest

from synode.fabricator import fabricator, prompt_validation, smoke, synthesis


def complete_navigation(run_dir: Path) -> None:
    payload = json.loads((run_dir / "navigation-evidence.json").read_text(encoding="utf-8"))
    payload.update(
        {
            "status": "complete",
            "mcp_tools_used": [],
            "fallback_commands": ["rg -n fabricator src/synode/fabricator docs/fabricator"],
            "not_used": ["Unit test uses local fixture state only."],
            "findings": ["Fabricator test scope is limited to the Synode developer workflow."],
        }
    )
    (run_dir / "navigation-evidence.json").write_text(json.dumps(payload), encoding="utf-8")
    (run_dir / "navigation-evidence.md").write_text(
        "# Fabricator Navigation Evidence\n\n## Findings\n\n- Unit test navigation is complete.\n",
        encoding="utf-8",
    )


def test_validate_all_checks_registry_routing_prompts_and_templates() -> None:
    result = fabricator.validate_all()
    expert_count = len(fabricator.validate_experts(fabricator.load_toml(fabricator.EXPERTS_PATH)))

    assert result == {
        "ok": True,
        "experts": expert_count,
        "profiles": 10,
        "prompts": expert_count,
        "persona_sections": expert_count * len(fabricator.REQUIRED_PROMPT_SECTIONS),
        "stance_packs": expert_count,
        "stance_sections": expert_count * len(fabricator.REQUIRED_STANCE_SECTIONS),
        "templates": len(fabricator.REQUIRED_TEMPLATES),
    }


def test_prompt_pack_has_required_persona_and_stance_sections() -> None:
    experts = fabricator.validate_experts(fabricator.load_toml(fabricator.EXPERTS_PATH))

    for expert_id in experts:
        prompt_path = fabricator.PROMPTS_DIR / f"{expert_id}.md"
        prompt_sections = prompt_validation.prompt_sections(prompt_path.read_text(encoding="utf-8"))
        assert set(fabricator.REQUIRED_PROMPT_SECTIONS) <= prompt_sections

        stance_path = fabricator.STANCE_PACKS_DIR / f"{expert_id}.md"
        stance_sections = prompt_validation.stance_sections(stance_path.read_text(encoding="utf-8"))
        assert set(fabricator.REQUIRED_STANCE_SECTIONS) <= stance_sections


def test_start_run_creates_synode_developer_tooling_workspace(tmp_path: Path) -> None:
    result = fabricator.start_run(
        mode="plan-patch",
        goal="Add a Synode Fabricator smoke check",
        paths=["src/synode/fabricator/fabricator.py"],
        profile_id="developer_tooling",
        run_id="unit-run",
        runs_dir=tmp_path,
    )

    run_dir = Path(result["run_dir"])
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    arbiter_prompt = run_dir / "expert-prompts" / "principal_arbiter.md"

    assert run["mode"] == "plan-patch"
    assert run["profile"] == "developer_tooling"
    assert run["selected_experts"] == [
        "principal_arbiter",
        "developer_docs_reviewer",
        "qa_regression_strategist",
        "complexity_controller",
    ]
    assert run["required_docs"] == ["agents.md", "docs/verification.md", "architecture.yml"]
    assert run["git_policy"]["allowed_working_tree_edits"] is True
    assert {"commit", "push", "open_pr", "open_mr"} <= set(run["git_policy"]["forbidden_actions"])
    assert (run_dir / "navigation-evidence.md").exists()
    assert (run_dir / "selection.md").exists()
    assert (run_dir / "challenge-brief.md").exists()
    assert (run_dir / "review-report.md").exists()
    prompt_text = arbiter_prompt.read_text(encoding="utf-8")
    assert "## Mission" in prompt_text
    assert "## Persona Stance Pack" in prompt_text
    assert "Do not commit, push, open PR, open MR" in prompt_text


@pytest.mark.parametrize(
    ("goal", "paths", "expected_profile"),
    [
        ("Move agent graph creation into workflows", ["web/src/app/workflows/page.tsx"], "operator_ui"),
        ("Harden MCP sandbox policy", ["src/synode/tools/base.py"], "tools_sandbox_mcp"),
        ("Add Alembic migration", ["alembic/versions/0002_example.py"], "persistence_migration"),
        ("Fix Ollama model profile routing", ["src/synode/models/provider.py"], "model_provider_profiles"),
        ("Update local deployment docs", ["docker-compose.yaml"], "local_deployment"),
    ],
)
def test_start_run_infers_synode_profiles(
    tmp_path: Path,
    goal: str,
    paths: list[str],
    expected_profile: str,
) -> None:
    fabricator.start_run(
        mode="plan-only",
        goal=goal,
        paths=paths,
        profile_id=None,
        run_id=expected_profile,
        runs_dir=tmp_path,
    )

    run = json.loads((tmp_path / expected_profile / "run.json").read_text(encoding="utf-8"))
    assert run["profile"] == expected_profile


def test_start_run_accepts_explicit_synode_expert_override(tmp_path: Path) -> None:
    override = [
        "tool_policy_sandbox_reviewer",
        "security_boundary_reviewer",
        "qa_regression_strategist",
        "red_team_reviewer",
    ]
    reason = "Sandbox and approval behavior crosses policy, security, testing, and adversarial review."

    result = fabricator.start_run(
        mode="plan-only",
        goal="Audit Synode sandbox approval boundary",
        paths=["src/synode/tools/base.py", "src/synode/runtime/sandbox.py"],
        profile_id="tools_sandbox_mcp",
        run_id="override-run",
        runs_dir=tmp_path,
        expert_override_ids=override,
        expert_override_reason=reason,
    )

    run_dir = Path(result["run_dir"])
    run = json.loads((run_dir / "run.json").read_text(encoding="utf-8"))
    selection = (run_dir / "selection.md").read_text(encoding="utf-8")

    assert run["selected_experts"] == ["principal_arbiter", *override]
    assert run["expert_selection_source"] == "explicit_override"
    assert run["expert_override_reason"] == reason
    assert "Expert selection source: `explicit_override`" in selection
    assert reason in selection


@pytest.mark.parametrize(
    ("expert_override_ids", "expert_override_reason", "match"),
    [
        (["missing_expert"], "Need a custom expert set.", "unknown experts"),
        (["qa_regression_strategist", "qa_regression_strategist"], "Need a custom expert set.", "duplicate experts"),
        (["principal_arbiter"], "Need a custom expert set.", "must not include principal_arbiter"),
        ([], "Need a custom expert set.", "at least one optional expert"),
        (["qa_regression_strategist"], None, "expert_override_reason is required"),
        (None, "Reason without experts.", "requires expert override ids"),
    ],
)
def test_start_run_rejects_invalid_expert_overrides(
    tmp_path: Path,
    expert_override_ids: list[str] | None,
    expert_override_reason: str | None,
    match: str,
) -> None:
    with pytest.raises(fabricator.FabricatorError, match=match):
        fabricator.start_run(
            mode="plan-patch",
            goal="Reject bad Fabricator override",
            paths=["src/synode/fabricator/fabricator.py"],
            profile_id="developer_tooling",
            run_id="bad-override",
            runs_dir=tmp_path,
            expert_override_ids=expert_override_ids,
            expert_override_reason=expert_override_reason,
        )


def test_synthesis_uses_synode_finding_clusters() -> None:
    source_items = []
    for response in (
        {
            "expert_id": "tool_policy_sandbox_reviewer",
            "phase": "expert",
            "verdict": "block",
            "blockers": ["Docker sandbox isolation must fail closed before filesystem write tools run."],
            "advisory_findings": [],
            "required_constraints": [],
            "verification_implications": ["Add sandbox unavailable tests."],
            "challenged_recommendations": [],
            "decision_impact": "Arbiter must require sandbox fail-closed behavior.",
        },
        {
            "expert_id": "security_boundary_reviewer",
            "phase": "expert",
            "verdict": "revise",
            "blockers": [],
            "advisory_findings": [],
            "required_constraints": ["Approval decisions must not bypass role allowlist or workspace policy."],
            "verification_implications": ["Add approval policy regression tests."],
            "challenged_recommendations": [],
            "decision_impact": "Arbiter must preserve approval policy constraints.",
        },
    ):
        source_items.extend(synthesis.source_items_for_response(response, include_advisory=True))
    clusters = synthesis.finding_clusters_from_source_items(source_items)

    cluster_ids = {cluster["id"] for cluster in clusters}
    assert "cluster-sandbox-isolation" in cluster_ids
    assert "cluster-approval-tool-policy" in cluster_ids


def test_smoke_completes_local_fabricator_workflow(tmp_path: Path) -> None:
    result = smoke.smoke(tmp_path / "fabricator-smoke-runs")

    assert result["ok"] is True
    assert result["validate"]["ok"] is True
    assert result["navigation"] == {"ok": True, "status": "complete"}
    assert result["finalize"]["status"] == "completed"


def test_fabricator_sources_do_not_reference_old_domain() -> None:
    forbidden = [
        "stream" + "_miner",
        "stream" + "-miner",
        "Stream " + "Miner",
        "Developer " + "Center",
        "Control " + "Plane",
        "Jet" + "Stream",
        "Stats " + "Forge",
    ]
    roots = [fabricator.REPO_ROOT / "src" / "synode" / "fabricator", fabricator.FABRICATOR_DOCS]
    offenders: list[str] = []

    for root in roots:
        for path in root.rglob("*"):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                text = path.read_text(encoding="utf-8")
                for needle in forbidden:
                    if needle in text:
                        offenders.append(f"{path.relative_to(fabricator.REPO_ROOT)}: {needle}")

    assert offenders == []
