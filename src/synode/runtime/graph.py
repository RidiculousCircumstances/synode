from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Send
from pydantic import BaseModel

from synode.models.provider import ModelProviderRegistry, ModelRequest
from synode.persistence.database import Database
from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.runtime.decisions import (
    WORKER_ROLES,
    CodingInspection,
    FilePatch,
    PatchProposal,
    ReviewerDecision,
    ReviewerVerdict,
    SupervisorDecision,
    VerificationPlan,
)
from synode.runtime.state import SynodeState
from synode.schemas import AgentOutput, EventType, RoleName, RunMode, ToolCall, ToolResult
from synode.tools.base import ToolExecutor


@dataclass(frozen=True)
class GraphDependencies:
    database: Database
    roles: RoleRegistry
    models: ModelProviderRegistry
    tool_executor: ToolExecutor


def build_graph(deps: GraphDependencies, checkpointer: Any | None = None) -> Any:
    builder = StateGraph(SynodeState)
    builder.add_node("intake", _intake_node(deps))
    builder.add_node("supervisor", _supervisor_node(deps))
    builder.add_node("worker", _worker_node(deps))
    builder.add_node("coding_inspect", _coding_inspect_node(deps))
    builder.add_node("coding_patch_propose", _coding_patch_propose_node(deps))
    builder.add_node("patch_apply", _patch_apply_node(deps))
    builder.add_node("verify", _verify_node(deps))
    builder.add_node("reviewer", _reviewer_node(deps))
    builder.add_node("synthesizer", _synthesizer_node(deps))
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "supervisor")
    builder.add_conditional_edges("supervisor", _route_after_supervisor)
    builder.add_conditional_edges("worker", _after_worker, {"reviewer": "reviewer"})
    builder.add_edge("coding_inspect", "coding_patch_propose")
    builder.add_edge("coding_patch_propose", "patch_apply")
    builder.add_edge("patch_apply", "verify")
    builder.add_edge("verify", "reviewer")
    builder.add_edge("reviewer", "synthesizer")
    builder.add_edge("synthesizer", END)
    return builder.compile(checkpointer=checkpointer)


def _intake_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_event(
                state["run_id"], "intake_completed", None, {"task": state["task"], "mode": state["mode"]}
            )
        return {}

    return node


def _supervisor_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        provider = deps.models.get(state["model_provider"])
        decision = await _invoke_structured(
            provider,
            SupervisorDecision,
            ModelRequest(
                role=RoleName.SUPERVISOR.value,
                prompt=_supervisor_prompt(state, deps),
                context={"mode": state["mode"], "task": state["task"]},
                response_schema=SupervisorDecision,
            ),
        )
        _validate_supervisor_decision(decision, deps)
        role_tool_calls = {
            step.role.value: [call.model_dump(mode="json") for call in step.tool_calls]
            for step in decision.plan
        }
        plan = [
            {"role": step.role.value, "task": step.task, "tool_calls": role_tool_calls[step.role.value]}
            for step in decision.plan
        ]
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(
                state["run_id"], "supervisor_decision", decision.model_dump(mode="json")
            )
            for role in decision.selected_roles:
                await repo.add_event(state["run_id"], EventType.ROLE_SELECTED.value, role.value, {"role": role.value})
        return {
            "selected_roles": [role.value for role in decision.selected_roles],
            "plan": plan,
            "role_tool_calls": role_tool_calls,
        }

    return node


def _route_after_supervisor(state: SynodeState) -> list[Send] | str:
    if state["mode"] == RunMode.CODING.value:
        return "coding_inspect"
    return [
        Send("worker", {**state, "current_role": role})
        for role in state.get("selected_roles", [])
    ]


def _after_worker(state: SynodeState) -> str:
    return "reviewer"


def _worker_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        role = state["current_role"]
        calls = [
            ToolCall.model_validate(call)
            for call in state.get("role_tool_calls", {}).get(role, [])
        ]
        results = []
        for call in calls:
            result = await deps.tool_executor.execute(state["run_id"], role, state.get("workspace"), call)
            results.append(result)
        provider = deps.models.get(state["model_provider"])
        model_response = await provider.invoke(
            ModelRequest(
                role=role,
                prompt=f"Summarize work for task: {state['task']}",
                context={"tool_results": [result.model_dump(mode="json") for result in results]},
                tools=[call.name for call in calls],
            )
        )
        output = AgentOutput(
            role=role,
            summary=_summarize_role_output(role, model_response.content, results),
            tool_results=results,
            risks=[result.error for result in results if result.error],
        )
        return {"worker_outputs": [output.model_dump(mode="json")]}

    return node


def _coding_inspect_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        seed_calls = [
            ToolCall(name="native.git_status", arguments={}),
            ToolCall(
                name="native.fs_search",
                arguments={"pattern": "TODO|FIXME|error|raise|def |class ", "glob": "*.py", "max_matches": 40},
            ),
        ]
        results: list[ToolResult] = []
        for call in seed_calls:
            results.append(await deps.tool_executor.execute(state["run_id"], RoleName.CODER.value, state.get("workspace"), call))
        provider = deps.models.get(state["model_provider"])
        inspection = await _invoke_structured(
            provider,
            CodingInspection,
            ModelRequest(
                role=RoleName.CODER.value,
                prompt="Inspect repository evidence and identify files/tests needed for the coding task.",
                context={"task": state["task"], "tool_results": [result.model_dump(mode="json") for result in results]},
                response_schema=CodingInspection,
            ),
        )
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(state["run_id"], "coding_inspection", inspection.model_dump(mode="json"))
        return {"coding_inspection": inspection.model_dump(mode="json")}

    return node


def _coding_patch_propose_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        async with deps.database.session() as session:
            repo = Repository(session)
            existing = await repo.get_latest_artifact(state["run_id"], "patch_proposal")
            if existing is not None:
                return {"patch_proposal": existing.content}

        file_context = await _read_relevant_files(deps, state)
        provider = deps.models.get(state["model_provider"])
        context: dict[str, Any] = {
            "task": state["task"],
            "inspection": state.get("coding_inspection", {}),
            "files": file_context,
        }
        if state["model_provider"] == "fake":
            context["fake_patch_proposal"] = _fake_patch_proposal(file_context)
        proposal = await _invoke_structured(
            provider,
            PatchProposal,
            ModelRequest(
                role=RoleName.CODER.value,
                prompt="Propose a minimal patch for the coding task using the provided file contents.",
                context=context,
                response_schema=PatchProposal,
            ),
        )
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(state["run_id"], "patch_proposal", proposal.model_dump(mode="json"))
        return {"patch_proposal": proposal.model_dump(mode="json")}

    return node


def _patch_apply_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        proposal = PatchProposal.model_validate(state["patch_proposal"])
        result = await deps.tool_executor.execute(
            state["run_id"],
            RoleName.CODER.value,
            state.get("workspace"),
            ToolCall(
                name="native.patch_apply",
                arguments={"patches": [patch.model_dump(mode="json") for patch in proposal.patches]},
            ),
        )
        results = [result.model_dump(mode="json")]
        if result.ok:
            diff = await deps.tool_executor.execute(
                state["run_id"], RoleName.CODER.value, state.get("workspace"), ToolCall(name="native.git_diff", arguments={})
            )
            results.append(diff.model_dump(mode="json"))
        return {"patch_results": results}

    return node


def _verify_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        patch_results = [ToolResult.model_validate(result) for result in state.get("patch_results", [])]
        if not patch_results or not patch_results[0].ok:
            return {"verification_result": {"skipped": True, "reason": "patch did not apply"}}
        proposal = PatchProposal.model_validate(state["patch_proposal"])
        provider = deps.models.get(state["model_provider"])
        plan = await _invoke_structured(
            provider,
            VerificationPlan,
            ModelRequest(
                role=RoleName.CODER.value,
                prompt="Choose focused verification commands for the applied patch.",
                context={"commands": proposal.verification_commands, "patch_proposal": proposal.model_dump(mode="json")},
                response_schema=VerificationPlan,
            ),
        )
        result = await deps.tool_executor.execute(
            state["run_id"],
            RoleName.CODER.value,
            state.get("workspace"),
            ToolCall(name="native.verify", arguments={"commands": plan.commands}),
        )
        return {"verification_result": result.model_dump(mode="json")}

    return node


def _reviewer_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        blockers, advisory = _precheck_review_findings(state)
        provider = deps.models.get(state["model_provider"])
        decision = await _invoke_structured(
            provider,
            ReviewerDecision,
            ModelRequest(
                role=RoleName.REVIEWER.value,
                prompt="Review worker outputs, patch results, verification, and policy signals.",
                context={"blockers": blockers, "advisory": advisory, "state": _compact_state_for_review(state)},
                response_schema=ReviewerDecision,
            ),
        )
        merged_blockers = [*blockers, *decision.blockers]
        verdict = ReviewerVerdict.BLOCK if merged_blockers else decision.verdict
        review = {
            **decision.model_dump(mode="json"),
            "verdict": verdict.value,
            "blockers": merged_blockers,
            "advisory_risks": [*advisory, *decision.advisory_risks],
            "can_proceed": verdict == ReviewerVerdict.PROCEED,
        }
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(state["run_id"], "reviewer_decision", review)
        return {"review": review}

    return node


def _synthesizer_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        lines = ["Synode run summary:"]
        lines.append(f"Mode: {state['mode']}")
        for step in state.get("plan", []):
            lines.append(f"- {step['role']}: {step['task']}")
        for output in state.get("worker_outputs", []):
            lines.append(f"\n[{output['role']}]\n{output['summary']}")
        if state.get("coding_inspection"):
            lines.append(f"\n[coding_inspection]\n{json.dumps(state['coding_inspection'], ensure_ascii=False)}")
        if state.get("patch_proposal"):
            lines.append(f"\n[patch_proposal]\n{json.dumps(state['patch_proposal'], ensure_ascii=False)}")
        if state.get("patch_results"):
            lines.append(f"\n[patch_results]\n{json.dumps(state['patch_results'], ensure_ascii=False)}")
        if state.get("verification_result"):
            lines.append(f"\n[verification]\n{json.dumps(state['verification_result'], ensure_ascii=False)}")
        review = state.get("review", {})
        if review.get("blockers"):
            lines.append("\nBlockers:")
            lines.extend(f"- {item}" for item in review["blockers"])
        if review.get("advisory_risks"):
            lines.append("\nAdvisory risks:")
            lines.extend(f"- {item}" for item in review["advisory_risks"])
        final = "\n".join(lines)
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(state["run_id"], "final_answer", {"text": final})
        return {"final_answer": final}

    return node


async def _invoke_structured(provider: Any, schema: type[BaseModel], request: ModelRequest) -> Any:
    response = await provider.invoke(request)
    return schema.model_validate(response.structured)


def _validate_supervisor_decision(decision: SupervisorDecision, deps: GraphDependencies) -> None:
    for role in decision.selected_roles:
        if role not in WORKER_ROLES:
            raise ValueError(f"supervisor selected non-worker role: {role.value}")
        deps.roles.get(role.value)
    for step in decision.plan:
        if step.role not in WORKER_ROLES:
            raise ValueError(f"supervisor planned non-worker role: {step.role.value}")
        deps.roles.get(step.role.value)
        for call in step.tool_calls:
            deps.tool_executor.tools.get(call.name)
            if not deps.roles.get(step.role.value).allows_tool(call.name):
                raise PermissionError(f"role '{step.role.value}' is not allowed to use tool '{call.name}'")


def _supervisor_prompt(state: SynodeState, deps: GraphDependencies) -> str:
    roles = _worker_role_catalog(deps)
    return (
        "Create a strict executable plan for Synode.\n"
        f"Mode: {state['mode']}\n"
        f"Task: {state['task']}\n"
        f"Workspace: {state.get('workspace') or '<none>'}\n"
        f"Selectable worker roles: {json.dumps(roles, ensure_ascii=False)}\n"
        "selected_roles and every plan item MUST use only selectable worker roles.\n"
        "Do not include supervisor or reviewer in selected_roles or plan.\n"
        "tool_calls MUST use only allowed_tools listed on that same role.\n"
        "Do not invent tool names and do not use wildcard names such as mcp.* directly.\n"
        "When a useful tool call is uncertain, use an empty tool_calls list.\n"
        "For native.data_profile, use arguments {} to inspect the first CSV/JSON in the workspace.\n"
        "The set of plan.role values MUST exactly match selected_roles.\n"
        "Return only the requested structured JSON."
    )


def _worker_role_catalog(deps: GraphDependencies) -> list[dict[str, Any]]:
    concrete_tools = deps.tool_executor.tools.list_names()
    roles = []
    for role in deps.roles.as_public():
        role_name = RoleName(role["name"])
        if role_name not in WORKER_ROLES:
            continue
        spec = deps.roles.get(role_name.value)
        roles.append(
            {
                "name": role["name"],
                "mission": role["mission"],
                "allowed_tools": [tool for tool in concrete_tools if spec.allows_tool(tool)],
            }
        )
    return roles


async def _read_relevant_files(deps: GraphDependencies, state: SynodeState) -> list[dict[str, Any]]:
    inspection = CodingInspection.model_validate(state.get("coding_inspection", {}))
    files: list[dict[str, Any]] = []
    for path in inspection.relevant_files[:3]:
        result = await deps.tool_executor.execute(
            state["run_id"],
            RoleName.CODER.value,
            state.get("workspace"),
            ToolCall(name="native.fs_read", arguments={"path": path, "max_bytes": 12000}),
        )
        if result.ok:
            content = str(result.output["content"])
            files.append(
                {
                    "path": path,
                    "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                    "content": content,
                }
            )
    if not files:
        raise FileNotFoundError("coding inspection did not produce readable relevant files")
    return files


def _fake_patch_proposal(file_context: list[dict[str, Any]]) -> dict[str, Any]:
    target = file_context[0]
    content = str(target["content"])
    new_content = content.rstrip() + "\n\nSynode coding workflow smoke.\n"
    return PatchProposal(
        summary="Append a deterministic smoke line.",
        patches=[
            FilePatch(path=target["path"], expected_sha256=target["sha256"], old_text=content, new_text=new_content)
        ],
        verification_commands=[["python", "-m", "pytest"]],
    ).model_dump(mode="json")


def _precheck_review_findings(state: SynodeState) -> tuple[list[str], list[str]]:
    blockers: list[str] = []
    advisory: list[str] = []
    for output in state.get("worker_outputs", []):
        for result in output.get("tool_results", []):
            _classify_tool_result(result, blockers, advisory)
    for result in state.get("patch_results", []):
        _classify_tool_result(result, blockers, advisory)
    verification = state.get("verification_result")
    if verification:
        if verification.get("skipped"):
            blockers.append(str(verification.get("reason", "verification skipped")))
        elif not verification.get("ok"):
            blockers.append("verification failed")
    return blockers, advisory


def _classify_tool_result(result: dict[str, Any], blockers: list[str], advisory: list[str]) -> None:
    if result.get("approval_id"):
        blockers.append(f"Approval required for {result['tool_name']}: {result['approval_id']}")
    elif not result.get("ok"):
        blockers.append(f"{result.get('tool_name')} failed: {result.get('error')}")


def _compact_state_for_review(state: SynodeState) -> dict[str, Any]:
    return {
        "mode": state.get("mode"),
        "plan": state.get("plan", []),
        "worker_outputs": state.get("worker_outputs", []),
        "patch_results": state.get("patch_results", []),
        "verification_result": state.get("verification_result", {}),
    }


def _summarize_role_output(role: str, model_content: str, results: list[Any]) -> str:
    lines = [model_content]
    for result in results:
        if result.ok:
            lines.append(f"- {result.tool_name}: ok {result.output}")
        else:
            lines.append(f"- {result.tool_name}: failed {result.error}")
    return "\n".join(lines)
