from __future__ import annotations

import os
from typing import Any

from synode.fabricator.common import FabricatorError

MAX_ACTIVE_AGENTS_ENV = "FABRICATOR_MAX_ACTIVE_AGENTS"
MIN_AVAILABLE_MB_ENV = "FABRICATOR_MIN_AVAILABLE_MB"
DEFAULT_MAX_ACTIVE_AGENTS = 2
DEFAULT_MIN_AVAILABLE_MB = 4096
RESOURCE_CHECK_HISTORY_LIMIT = 20
HEAVY_COMMAND_POLICY = (
    "Expert agents must not run build, test, index, browser, docker, or broad "
    "repository scan commands by default. The Arbiter runs expensive checks "
    "centrally and sequentially when they are required."
)


def default_policy() -> dict[str, Any]:
    return {
        "max_active_agents": DEFAULT_MAX_ACTIVE_AGENTS,
        "min_available_memory_mb": DEFAULT_MIN_AVAILABLE_MB,
        "heavy_command_policy": HEAVY_COMMAND_POLICY,
    }


def effective_policy(run: dict[str, Any]) -> dict[str, Any]:
    configured = dict(default_policy())
    configured.update(run.get("resource_policy") or {})
    configured["max_active_agents"] = env_int(
        MAX_ACTIVE_AGENTS_ENV,
        configured["max_active_agents"],
        minimum=1,
    )
    configured["min_available_memory_mb"] = env_int(
        MIN_AVAILABLE_MB_ENV,
        configured["min_available_memory_mb"],
        minimum=0,
    )
    configured["heavy_command_policy"] = str(configured["heavy_command_policy"])
    return configured


def assert_can_dispatch(run: dict[str, Any], *, phase: str) -> dict[str, Any]:
    return record_check(run, action=f"dispatch-{phase}", phase=phase, expert_id=None, new_active_agents=0)


def assert_can_start_agent(run: dict[str, Any], *, phase: str, expert_id: str) -> dict[str, Any]:
    agent = run.get("agents", {}).get(phase, {}).get(expert_id, {})
    new_active_agents = 0 if agent.get("status") == "started" else 1
    return record_check(
        run,
        action="agent-started",
        phase=phase,
        expert_id=expert_id,
        new_active_agents=new_active_agents,
    )


def record_check(
    run: dict[str, Any],
    *,
    action: str,
    phase: str,
    expert_id: str | None,
    new_active_agents: int,
) -> dict[str, Any]:
    policy = effective_policy(run)
    active_agents = list_active_agents(run)
    projected_active_agents = len(active_agents) + new_active_agents
    max_active_agents = int(policy["max_active_agents"])
    if projected_active_agents > max_active_agents:
        active_text = ", ".join(
            f"{item['phase']}/{item['expert_id']}" for item in active_agents
        ) or "none"
        raise FabricatorError(
            f"Fabricator resource guard blocked {action}: active agents would be "
            f"{projected_active_agents}, above max {max_active_agents}. Complete "
            f"or fail an active agent before starting another. Active agents: {active_text}"
        )

    available_memory_mb = available_memory_mb_or_fail()
    min_available_mb = int(policy["min_available_memory_mb"])
    if available_memory_mb < min_available_mb:
        raise FabricatorError(
            f"Fabricator resource guard blocked {action}: available memory is "
            f"{available_memory_mb} MB, below required {min_available_mb} MB"
        )

    check = {
        "action": action,
        "phase": phase,
        "expert_id": expert_id,
        "available_memory_mb": available_memory_mb,
        "min_available_memory_mb": min_available_mb,
        "active_agents": len(active_agents),
        "projected_active_agents": projected_active_agents,
        "max_active_agents": max_active_agents,
        "heavy_command_policy": policy["heavy_command_policy"],
    }
    checks = run.setdefault("resource_checks", [])
    checks.append(check)
    if len(checks) > RESOURCE_CHECK_HISTORY_LIMIT:
        del checks[:-RESOURCE_CHECK_HISTORY_LIMIT]
    run["resource_policy_effective"] = policy
    return check


def list_active_agents(run: dict[str, Any]) -> list[dict[str, str]]:
    active = []
    for phase, phase_agents in run.get("agents", {}).items():
        for expert_id, agent in phase_agents.items():
            if agent.get("status") == "started":
                active.append({"phase": phase, "expert_id": expert_id})
    return active


def available_memory_mb_or_fail() -> int:
    available = read_proc_mem_available_mb()
    if available is not None:
        return available
    available = read_sysconf_available_mb()
    if available is not None:
        return available
    raise FabricatorError("Fabricator resource guard could not determine available memory")


def read_proc_mem_available_mb() -> int | None:
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemAvailable:"):
                    parts = line.split()
                    if len(parts) >= 2:
                        return int(parts[1]) // 1024
    except (FileNotFoundError, OSError, ValueError):
        return None
    return None


def read_sysconf_available_mb() -> int | None:
    try:
        pages = os.sysconf("SC_AVPHYS_PAGES")
        page_size = os.sysconf("SC_PAGE_SIZE")
    except (AttributeError, OSError, ValueError):
        return None
    if not isinstance(pages, int) or not isinstance(page_size, int):
        return None
    if pages < 0 or page_size <= 0:
        return None
    return pages * page_size // (1024 * 1024)


def env_int(name: str, default: int, *, minimum: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw == "":
        return int(default)
    try:
        value = int(raw)
    except ValueError as exc:
        raise FabricatorError(f"{name} must be an integer") from exc
    if value < minimum:
        raise FabricatorError(f"{name} must be >= {minimum}")
    return value
