from __future__ import annotations

from typing import Any

from synode.domain.models import ToolResult
from synode.domain.runtime.contracts import (
    CODING_INSPECTION_CONTRACT,
    CODING_PATCH_PROPOSAL_CONTRACT,
)
from synode.domain.runtime.loop_policy import NativeLoopMode

DISCOVERY_TOOL_NAMES = frozenset(
    {
        "native.fs_list",
        "native.fs_search",
        "native.fs_read",
        "native.git_status",
        "native.git_diff",
        "native.data_profile",
    }
)
MUTATION_TOOL_NAMES = frozenset({"native.fs_write", "native.patch_apply"})
VERIFICATION_TOOL_NAMES = frozenset({"native.verify", "native.shell", "native.git_status", "native.git_diff"})
CODING_CONTRACT_IDS = frozenset({CODING_INSPECTION_CONTRACT, CODING_PATCH_PROPOSAL_CONTRACT})


def native_loop_phase(
    policy_mode: NativeLoopMode,
    *,
    contract_id: str,
    trace: list[dict[str, Any]],
    tool_results: list[ToolResult],
    validation_errors: list[str],
) -> str:
    del policy_mode
    if validation_errors or any(not result.ok for result in tool_results[-1:]):
        return "repair"
    if not tool_results:
        return "inspect"
    last_mutation = _last_tool_index(tool_results, MUTATION_TOOL_NAMES)
    last_verification = _last_tool_index(tool_results, VERIFICATION_TOOL_NAMES)
    if last_mutation is not None and (last_verification is None or last_verification < last_mutation):
        return "verify"
    if contract_id == CODING_PATCH_PROPOSAL_CONTRACT and not _trace_has_finish_attempt(trace):
        return "patch"
    return "finish_ready"


def native_loop_allowed_tools_for_phase(
    allowed_tools: list[str],
    *,
    policy_mode: NativeLoopMode,
    phase: str,
) -> list[str]:
    if policy_mode != "strict":
        return allowed_tools
    allowed = set(allowed_tools)
    if phase == "inspect":
        names = DISCOVERY_TOOL_NAMES
    elif phase == "verify":
        names = DISCOVERY_TOOL_NAMES | VERIFICATION_TOOL_NAMES
    elif phase == "repair":
        names = DISCOVERY_TOOL_NAMES | MUTATION_TOOL_NAMES | VERIFICATION_TOOL_NAMES
    else:
        names = DISCOVERY_TOOL_NAMES | MUTATION_TOOL_NAMES | VERIFICATION_TOOL_NAMES
    filtered = [name for name in allowed_tools if name in names]
    return filtered or [name for name in allowed_tools if name in allowed]


def native_loop_tool_policy_error(
    role: str,
    tool_name: str,
    allowed_tools: list[str],
    effective_allowed_tools: list[str],
    phase: str,
) -> str:
    if tool_name in allowed_tools:
        return (
            f"tool {tool_name} is not available during native_loop_phase={phase}. "
            f"Use one of the phase-allowed tools: {effective_allowed_tools}"
        )
    return f"tool is not allowed for role {role}: {tool_name}"


def native_loop_duplicate_fail_after(policy_mode: NativeLoopMode) -> int:
    if policy_mode == "strict":
        return 2
    if policy_mode == "guided":
        return 3
    return 4


def native_loop_finish_gate_error(
    policy_mode: NativeLoopMode,
    *,
    contract_id: str,
    trace: list[dict[str, Any]],
    tool_results: list[ToolResult],
) -> str | None:
    successful_tools = [result for result in tool_results if result.ok]
    if policy_mode == "strict" and not successful_tools:
        return "finish is premature in strict mode: make at least one successful tool_call before finish"
    if policy_mode == "guided" and contract_id in CODING_CONTRACT_IDS and not successful_tools:
        return "finish is premature for coding contracts in guided mode: inspect the workspace with a tool_call first"
    last_mutation = _last_tool_index(tool_results, MUTATION_TOOL_NAMES)
    if last_mutation is None:
        return None
    last_verification = _last_tool_index(tool_results, VERIFICATION_TOOL_NAMES)
    if policy_mode in {"strict", "guided"} and (last_verification is None or last_verification < last_mutation):
        return "finish is premature after a mutation: run native.verify or another verification tool first"
    if policy_mode == "autonomous" and not _trace_has_verification_after_mutation(trace, tool_results):
        return "finish after mutation needs verification evidence"
    return None


def native_loop_state_summary(
    trace: list[dict[str, Any]],
    tool_results: list[ToolResult],
    validation_errors: list[str],
) -> dict[str, Any]:
    successful_tools = [result for result in tool_results if result.ok]
    files_seen = _native_loop_files_seen(trace, tool_results)
    verification_failures = verification_failures_from_results(tool_results)
    return {
        "successful_tool_calls": len(successful_tools),
        "last_tool": tool_results[-1].tool_name if tool_results else None,
        "last_tool_ok": tool_results[-1].ok if tool_results else None,
        "mutations_seen": [result.tool_name for result in tool_results if result.tool_name in MUTATION_TOOL_NAMES],
        "verification_seen": [result.tool_name for result in tool_results if result.tool_name in VERIFICATION_TOOL_NAMES],
        "files_seen": files_seen[-12:],
        "recent_validation_errors": validation_errors[-3:],
        "recent_trace_errors": [str(step.get("error")) for step in trace[-5:] if step.get("error")],
        "verification_failures": verification_failures[-10:],
    }


def verification_failures_from_results(tool_results: list[ToolResult]) -> list[str]:
    failures: list[str] = []
    for result in tool_results:
        if result.tool_name not in VERIFICATION_TOOL_NAMES:
            continue
        if result.ok:
            continue
        chunks = [result.error or ""]
        for key in ("stdout", "stderr", "output", "combined_output"):
            value = result.output.get(key)
            if isinstance(value, str):
                chunks.append(value)
        failures.extend(parse_verification_failures("\n".join(chunks)))
    return failures


def parse_verification_failures(text: str) -> list[str]:
    markers = ("FAILED ", "AssertionError", "Error:", "Traceback", "expected", "actual")
    failures: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if any(marker in line for marker in markers):
            failures.append(line[:240])
        if len(failures) >= 12:
            break
    return failures


def native_loop_training_transitions(trace: list[dict[str, Any]]) -> list[dict[str, Any]]:
    transitions: list[dict[str, Any]] = []
    for step in trace:
        action = step.get("action")
        if action not in {"tool_call", "finish", "needs_operator", "invalid_action"}:
            continue
        transitions.append(
            {
                "step": step.get("step"),
                "policy_mode": step.get("policy_mode"),
                "phase": step.get("phase"),
                "action": action,
                "tool_name": (step.get("tool_call") or {}).get("name") if isinstance(step.get("tool_call"), dict) else None,
                "ok": "error" not in step,
                "error": step.get("error"),
            }
        )
    return transitions


def _native_loop_files_seen(trace: list[dict[str, Any]], tool_results: list[ToolResult]) -> list[str]:
    files: list[str] = []
    for step in trace:
        raw_call = step.get("tool_call")
        call = raw_call if isinstance(raw_call, dict) else {}
        raw_args = call.get("arguments")
        args = raw_args if isinstance(raw_args, dict) else {}
        for key in ("path", "glob"):
            value = args.get(key)
            if isinstance(value, str) and value:
                files.append(value)
    for result in tool_results:
        for key in ("path", "file", "files", "matches", "changed_files"):
            value = result.output.get(key)
            if isinstance(value, str) and value:
                files.append(value)
            elif isinstance(value, list):
                files.extend(str(item) for item in value[:8] if item)
    return list(dict.fromkeys(files))


def _last_tool_index(tool_results: list[ToolResult], names: frozenset[str]) -> int | None:
    for index in range(len(tool_results) - 1, -1, -1):
        if tool_results[index].tool_name in names and tool_results[index].ok:
            return index
    return None


def _trace_has_finish_attempt(trace: list[dict[str, Any]]) -> bool:
    return any(step.get("action") == "finish" for step in trace)


def _trace_has_verification_after_mutation(trace: list[dict[str, Any]], tool_results: list[ToolResult]) -> bool:
    del trace
    last_mutation = _last_tool_index(tool_results, MUTATION_TOOL_NAMES)
    if last_mutation is None:
        return True
    last_verification = _last_tool_index(tool_results, VERIFICATION_TOOL_NAMES)
    return last_verification is not None and last_verification > last_mutation
