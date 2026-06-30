from __future__ import annotations

import re
from typing import Any

SEVERITY_RANK = {"advisory": 0, "revise": 1, "blocker": 2}
TOPIC_RULES: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    (
        "approval-tool-policy",
        "Approval And Tool Policy",
        (
            "approval",
            "approve",
            "reject",
            "tool policy",
            "workspace policy",
            "role allowlist",
            "audit",
            "mutation",
            "side effect",
            "unsafe shell",
        ),
    ),
    (
        "sandbox-isolation",
        "Sandbox Isolation",
        (
            "sandbox",
            "docker",
            "container",
            "isolation",
            "network policy",
            "resource limit",
            "timeout",
            "filesystem write",
            "artifact",
            "escape",
        ),
    ),
    (
        "model-profile-routing",
        "Model Profile Routing",
        (
            "model profile",
            "provider",
            "ollama",
            "openai-compatible",
            "structured output",
            "streaming",
            "fallback",
            "profile binding",
            "model health",
        ),
    ),
    (
        "run-worker-lifecycle",
        "Run And Worker Lifecycle",
        (
            "thread",
            "run",
            "worker",
            "queue",
            "stale",
            "cancel",
            "resume",
            "checkpoint",
            "langgraph",
            "event",
            "heartbeat",
        ),
    ),
    (
        "persistence-retention",
        "Persistence And Retention",
        (
            "postgres",
            "sqlite",
            "migration",
            "alembic",
            "retention",
            "cleanup",
            "pagination",
            "index",
            "backup",
            "restore",
            "payload size",
        ),
    ),
    (
        "operator-ui-workflows",
        "Operator UI And Workflows",
        (
            "operator",
            "ui",
            "workflow",
            "agent graph",
            "role catalog",
            "settings",
            "approval dialog",
            "run status",
            "diagnostics",
            "layout",
        ),
    ),
    (
        "deployment-local-lan",
        "Local Deployment And LAN Boundary",
        (
            "docker compose",
            "compose",
            "healthcheck",
            "trusted local",
            "trusted lan",
            "public internet",
            "firewall",
            "reverse proxy",
            "ollama external",
        ),
    ),
    (
        "secret-boundary",
        "Secrets And Security Boundary",
        (
            "credential",
            "secret",
            "token",
            "secrets key",
            "encryption",
            "decrypt",
            "redact",
            "redaction",
            "sensitive",
            "password",
        ),
    ),
    (
        "observability-diagnostics",
        "Observability And Diagnostics",
        (
            "observability",
            "langfuse",
            "trace",
            "trace_id",
            "log",
            "diagnostic",
            "diagnostics",
            "failure reason",
            "latency",
            "token usage",
            "runtime status",
        ),
    ),
    (
        "mcp-boundary",
        "MCP Boundary",
        (
            "mcp",
            "server",
            "tool discovery",
            "side effect",
            "policy engine",
            "unavailable tool",
            "capability",
            "connector",
        ),
    ),
    (
        "state-docs-contract",
        "State And Docs Contract",
        (
            "docs",
            "documentation",
            "contract",
            "architecture",
            "continuity",
            "agents.md",
            "state vocabulary",
            "run status",
            "agent role",
            "graph snapshot",
        ),
    ),
    (
        "scope-control",
        "Scope Control",
        (
            "scope",
            "new service",
            "new queue",
            "new database",
            "new dashboard",
            "new make target",
            "framework",
            "ceremony",
            "dependency",
            "overengineering",
        ),
    ),
    (
        "verification",
        "Verification",
        (
            "verification",
            "test",
            "tests",
            "pytest",
            "make ",
            "coverage",
            "smoke",
            "lint",
            "typecheck",
        ),
    ),
)


def build_finding_cluster_payload(
    run: dict[str, Any],
    responses: list[dict[str, Any]],
    *,
    include_advisory: bool,
) -> dict[str, Any]:
    source_items = response_source_items(responses, include_advisory=include_advisory)
    clusters = finding_clusters_from_source_items(source_items)
    return {
        "schema_version": 1,
        "run_id": run["id"],
        "clusters": clusters,
        "source_items": source_items,
    }


def response_source_items(responses: list[dict[str, Any]], *, include_advisory: bool) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for response in responses:
        items.extend(source_items_for_response(response, include_advisory=include_advisory))
    return items


def source_items_for_response(response: dict[str, Any], *, include_advisory: bool) -> list[dict[str, Any]]:
    phase = response["phase"]
    expert_id = response["expert_id"]
    raw_items: list[tuple[str, str, str]] = []
    if response["verdict"] in {"revise", "block"}:
        raw_items.append(("verdict", "verdict", response["verdict"]))
    raw_items.extend(("blocker", "blockers", text) for text in response["blockers"])
    if include_advisory:
        raw_items.extend(("advisory", "advisory_findings", text) for text in response["advisory_findings"])
    raw_items.extend(("constraint", "required_constraints", text) for text in response["required_constraints"])
    raw_items.extend(
        ("challenged_recommendation", "challenged_recommendations", text)
        for text in response["challenged_recommendations"]
    )
    raw_items.extend(("verification", "verification_implications", text) for text in response["verification_implications"])

    counts: dict[str, int] = {}
    items = []
    for kind, field, text in raw_items:
        counts[kind] = counts.get(kind, 0) + 1
        item_id = f"{phase}-{expert_id}-{kind}-{counts[kind]}"
        topic = classify_finding_topic(text, kind)
        items.append(
            {
                "id": item_id,
                "source_phase": phase,
                "expert_id": expert_id,
                "kind": kind,
                "source_field": field,
                "text": text,
                "topic": topic["id"],
                "topic_title": topic["title"],
                "severity": source_item_severity(kind, text),
                "decision_impact": response.get("decision_impact", ""),
            }
        )
    return items


def finding_clusters_from_source_items(source_items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clusters_by_topic: dict[str, dict[str, Any]] = {}
    for item in source_items:
        topic = item["topic"]
        cluster = clusters_by_topic.setdefault(
            topic,
            {
                "id": f"cluster-{topic}",
                "topic": topic,
                "title": item["topic_title"],
                "severity": "advisory",
                "source_experts": [],
                "source_phases": [],
                "source_item_ids": [],
                "summary": "",
                "decision_impact": "",
                "required_tests": [],
                "item_count": 0,
                "_findings": [],
                "_decision_impacts": [],
            },
        )
        cluster["item_count"] += 1
        cluster["source_item_ids"].append(item["id"])
        append_unique(cluster["source_experts"], item["expert_id"])
        append_unique(cluster["source_phases"], item["source_phase"])
        if SEVERITY_RANK[item["severity"]] > SEVERITY_RANK[cluster["severity"]]:
            cluster["severity"] = item["severity"]
        if item["kind"] == "verification":
            append_unique(cluster["required_tests"], item["text"])
        else:
            cluster["_findings"].append(item["text"])
        if item["decision_impact"]:
            append_unique(cluster["_decision_impacts"], item["decision_impact"])

    clusters = []
    for topic in sorted(clusters_by_topic):
        cluster = clusters_by_topic[topic]
        findings = cluster.pop("_findings")
        decision_impacts = cluster.pop("_decision_impacts")
        if findings:
            cluster["summary"] = findings[0]
        elif cluster["required_tests"]:
            cluster["summary"] = cluster["required_tests"][0]
        else:
            cluster["summary"] = "No representative finding recorded."
        cluster["decision_impact"] = "; ".join(decision_impacts[:3]) if decision_impacts else "No decision impact recorded."
        clusters.append(cluster)
    clusters.sort(key=lambda item: (-SEVERITY_RANK[item["severity"]], item["id"]))
    return clusters


def classify_finding_topic(text: str, kind: str) -> dict[str, str]:
    normalized = normalize_topic_text(text)
    for topic_id, title, keywords in TOPIC_RULES:
        if any(keyword in normalized for keyword in keywords):
            return {"id": topic_id, "title": title}
    if kind == "verification":
        return {"id": "verification", "title": "Verification"}
    return {"id": "uncategorized", "title": "Uncategorized"}


def normalize_topic_text(text: str) -> str:
    lowered = text.lower().replace("`", " ")
    return re.sub(r"[^a-z0-9_./ -]+", " ", lowered)


def source_item_severity(kind: str, text: str) -> str:
    if kind == "blocker" or (kind == "verdict" and text == "block"):
        return "blocker"
    if kind in {"verdict", "constraint", "challenged_recommendation", "verification"}:
        return "revise"
    return "advisory"


def traceability_items_for_response(
    response: dict[str, Any],
    existing: dict[str, dict[str, Any]],
) -> list[dict[str, Any]]:
    items = []
    for source_item in source_items_for_response(response, include_advisory=False):
        item_id = source_item["id"]
        previous = existing.get(item_id, {})
        items.append(
            {
                "id": item_id,
                "source_phase": source_item["source_phase"],
                "expert_id": source_item["expert_id"],
                "kind": source_item["kind"],
                "source_field": source_item["source_field"],
                "text": source_item["text"],
                "cluster_id": f"cluster-{source_item['topic']}",
                "resolution": previous.get("resolution", "unresolved"),
                "resolution_reason": previous.get("resolution_reason", ""),
                "evidence": previous.get("evidence", ""),
            }
        )
    return items


def cluster_ids_for_source_items(clusters: list[dict[str, Any]]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    for cluster in clusters:
        for item_id in cluster.get("source_item_ids", []):
            result.setdefault(item_id, []).append(cluster["id"])
    return result


def render_traceability_markdown(payload: dict[str, Any]) -> str:
    if payload.get("clusters"):
        cluster_rows = [
            (
                f"- `{cluster['id']}` `{cluster['severity']}` from "
                f"{format_code_list(cluster['source_experts'])}: {cluster['summary']}"
            )
            for cluster in payload["clusters"]
        ]
    else:
        cluster_rows = ["- No finding clusters require tracking."]
    if not payload["items"]:
        rows = ["- No blocker, revise, constraint, challenge, or verification items require tracking."]
    else:
        rows = [
            (
                f"- `{item['id']}` `{item['resolution']}` from `{item['source_phase']}`/"
                f"`{item['expert_id']}` `{item['kind']}` via `{item['cluster_id']}`: {item['text']}"
            )
            for item in payload["items"]
        ]
    unresolved = [f"- `{item}`" for item in payload["unresolved"]] or ["- None."]
    unreferenced = [f"- `{item}`" for item in payload["unreferenced"]] or ["- None."]
    return "\n".join(
        [
            "# Decision Traceability",
            "",
            f"Run ID: `{payload['run_id']}`",
            "",
            "## Finding Clusters",
            "",
            *cluster_rows,
            "",
            "## Items",
            "",
            *rows,
            "",
            "## Unresolved",
            "",
            *unresolved,
            "",
            "## Not Referenced In Arbiter Artifacts",
            "",
            *unreferenced,
            "",
            "Resolution values must be `accepted`, `rejected`, or `human-decision` before `mark-decision-ready`.",
            "Mention each resolved item id or its cluster id in `arbiter-decision.md`, `implementation-handoff.md`, or `design-note.md`.",
            "",
        ]
    )


def render_synthesis_markdown(synthesis: dict[str, Any]) -> str:
    reasons = [f"- `{reason}`" for reason in synthesis["challenge_reasons"]] or ["- None."]
    candidates = [f"- `{expert_id}`" for expert_id in synthesis["challenge_candidates"]] or ["- None."]
    clusters = [
        (
            f"- `{cluster['id']}` `{cluster['severity']}`: {cluster['summary']} "
            f"(sources: {', '.join(cluster['source_experts'])})"
        )
        for cluster in synthesis.get("finding_clusters", [])
    ] or ["- None."]
    return "\n".join(
        [
            "# Arbiter Synthesis",
            "",
            f"Run ID: `{synthesis['run_id']}`",
            f"Challenge required: `{synthesis['challenge_required']}`",
            "",
            "## Challenge Reasons",
            "",
            *reasons,
            "",
            "## Challenge Candidates",
            "",
            *candidates,
            "",
            "## Finding Clusters",
            "",
            *clusters,
            "",
            "See `decision-brief.md` for the compact Arbiter briefing and `finding-clusters.json` for source item ids.",
            "",
        ]
    )


def render_finding_clusters_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Fabricator Finding Clusters",
        "",
        f"Run ID: `{payload['run_id']}`",
        "",
    ]
    if not payload["clusters"]:
        lines.extend(["- No finding clusters recorded.", ""])
        return "\n".join(lines)
    for cluster in payload["clusters"]:
        tests = [f"  - {item}" for item in cluster["required_tests"]] or ["  - None."]
        source_items = [f"`{item}`" for item in cluster["source_item_ids"]]
        lines.extend(
            [
                f"## `{cluster['id']}`",
                "",
                f"- Topic: {cluster['title']}",
                f"- Severity: `{cluster['severity']}`",
                f"- Source experts: {format_code_list(cluster['source_experts'])}",
                f"- Source item ids: {', '.join(source_items)}",
                f"- Summary: {cluster['summary']}",
                f"- Decision impact: {cluster['decision_impact']}",
                "- Required tests:",
                *tests,
                "",
            ]
        )
    return "\n".join(lines)


def render_decision_brief_markdown(run: dict[str, Any], synthesis: dict[str, Any]) -> str:
    clusters = synthesis.get("finding_clusters", [])
    blocker_clusters = [cluster for cluster in clusters if cluster["severity"] == "blocker"]
    revise_clusters = [cluster for cluster in clusters if cluster["severity"] == "revise"]
    advisory_clusters = [cluster for cluster in clusters if cluster["severity"] == "advisory"]

    challenge_reasons = [f"- `{reason}`" for reason in synthesis["challenge_reasons"]] or ["- None."]
    challenge_candidates = [f"- `{expert_id}`" for expert_id in synthesis["challenge_candidates"]] or ["- None."]
    return "\n".join(
        [
            "# Fabricator Decision Brief",
            "",
            f"Run ID: `{run['id']}`",
            f"Mode: `{run['mode']}`",
            f"Profile: `{run['profile']}`",
            "",
            "Use this brief before writing `challenge-brief.md`, `arbiter-decision.md`, or `implementation-handoff.md`.",
            "",
            "## Decision Pressure",
            "",
            f"- Challenge required: `{synthesis['challenge_required']}`",
            f"- Finding clusters: `{len(clusters)}`",
            "",
            "## Blocker Clusters",
            "",
            *render_cluster_list(blocker_clusters),
            "",
            "## Revise Clusters",
            "",
            *render_cluster_list(revise_clusters),
            "",
            "## Advisory Clusters",
            "",
            *render_cluster_list(advisory_clusters),
            "",
            "## Challenge",
            "",
            "Reasons:",
            "",
            *challenge_reasons,
            "",
            "Candidates:",
            "",
            *challenge_candidates,
            "",
            "## Arbiter Instruction",
            "",
            "- Decide at cluster level first; use raw source item ids only when a cluster needs finer resolution.",
            "- Reference accepted cluster ids in Arbiter artifacts to satisfy traceability without copying every raw item id.",
            "- Keep implementation boundaries narrower than the union of all advisory findings.",
            "",
        ]
    )


def render_cluster_list(clusters: list[dict[str, Any]]) -> list[str]:
    if not clusters:
        return ["- None."]
    return [
        f"- `{cluster['id']}`: {cluster['summary']} (sources: {', '.join(cluster['source_experts'])})"
        for cluster in clusters
    ]


def append_unique(values: list[str], value: str) -> None:
    if value not in values:
        values.append(value)


def format_code_list(values: list[str]) -> str:
    return ", ".join(f"`{item}`" for item in values) if values else "`none`"
