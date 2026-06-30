from __future__ import annotations

from typing import Any


def infer_profile(goal: str, paths: list[str], profiles: dict[str, dict[str, Any]]) -> dict[str, Any]:
    normalized_paths = [path.replace("\\", "/") for path in paths]
    goal_text = goal.lower()
    joined_paths = " ".join(normalized_paths).lower()
    if any(path.startswith("web/") for path in normalized_paths):
        return profiles["operator_ui"]
    if "migration" in goal_text or "alembic" in joined_paths or "migrations" in joined_paths:
        return profiles["persistence_migration"]
    if any(term in goal_text for term in ("model", "provider", "ollama", "profile", "structured output")):
        return profiles["model_provider_profiles"]
    if any(path.startswith(("src/synode/runtime/", "src/synode/graph", "src/synode/agents/")) for path in normalized_paths):
        return profiles["runtime_orchestration"]
    if any(path.startswith(("src/synode/tools/", ".mcp")) for path in normalized_paths) or "mcp" in goal_text:
        return profiles["tools_sandbox_mcp"]
    if any(term in goal_text for term in ("auth", "permission", "token", "security", "secret", "sandbox")):
        return profiles["security_boundary"]
    if any(path.startswith(("docker", "Dockerfile", "compose")) for path in normalized_paths) or "deployment" in goal_text:
        return profiles["local_deployment"]
    if normalized_paths and all(path.startswith("docs/") or path == "agents.md" for path in normalized_paths):
        return profiles["docs_process"]
    if any(path.startswith(("tools/", "tests/", "src/synode/fabricator/")) for path in normalized_paths):
        return profiles["developer_tooling"]
    return profiles["runtime_orchestration"]
