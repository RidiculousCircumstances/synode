from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any

from langgraph.constants import END, START
from langgraph.errors import GraphInterrupt
from langgraph.graph import StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel

from synode.models.provider import ModelProviderRegistry, ModelRequest
from synode.observability import Observability
from synode.persistence.database import Database
from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.runtime.contracts import WORKER_AGENT_OUTPUT_CONTRACT
from synode.runtime.decisions import (
    CodingInspection,
    FilePatch,
    PatchProposal,
    ReviewerDecision,
    ReviewerVerdict,
    SupervisorDecision,
    VerificationPlan,
)
from synode.runtime.execution import (
    ExecutionBackendRegistry,
    NodeExecutionInput,
    NodeExecutionOutput,
)
from synode.runtime.operator import ApprovalRequired, OperatorRejected, operator_interrupt_payload
from synode.runtime.state import SynodeState
from synode.schemas import (
    AgentOutput,
    EventType,
    InteractionMode,
    NodeExecutionStatus,
    OperatorRequestKind,
    OperatorResponseType,
    RoleName,
    RunMode,
    ToolCall,
    ToolResult,
)
from synode.security import SecretCipher
from synode.tools.base import ToolExecutor
from synode.tools.shell import is_safe_command


@dataclass(frozen=True)
class ResolvedModelProvider:
    provider: Any
    profile_id: str | None = None
    profile_name: str | None = None
    provider_type: str | None = None
    model_options: dict[str, Any] | None = None


@dataclass(frozen=True)
class GraphDependencies:
    database: Database
    roles: RoleRegistry
    models: ModelProviderRegistry
    tool_executor: ToolExecutor
    observability: Observability
    secret_cipher: SecretCipher | None = None
    execution_backends: ExecutionBackendRegistry | None = None


def build_graph(deps: GraphDependencies, checkpointer: Any | None = None) -> Any:
    builder = StateGraph(SynodeState)
    builder.add_node("intake", _observed_node("intake", deps, _intake_node(deps)))
    builder.add_node("supervisor", _observed_node("supervisor", deps, _supervisor_node(deps)))
    builder.add_node("graph_workers", _observed_node("graph_workers", deps, _graph_workers_node(deps)))
    builder.add_node("coding_inspect", _observed_node("coding_inspect", deps, _coding_inspect_node(deps)))
    builder.add_node(
        "coding_patch_propose",
        _observed_node("coding_patch_propose", deps, _coding_patch_propose_node(deps)),
    )
    builder.add_node(
        "coding_patch_repair",
        _observed_node("coding_patch_repair", deps, _coding_patch_propose_node(deps, repair_verification=True)),
    )
    builder.add_node("patch_apply", _observed_node("patch_apply", deps, _patch_apply_node(deps)))
    builder.add_node("verify", _observed_node("verify", deps, _verify_node(deps)))
    builder.add_node("reviewer", _observed_node("reviewer", deps, _reviewer_node(deps)))
    builder.add_node("synthesizer", _observed_node("synthesizer", deps, _synthesizer_node(deps)))
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "supervisor")
    builder.add_conditional_edges("supervisor", _route_after_supervisor)
    builder.add_edge("graph_workers", "reviewer")
    builder.add_edge("coding_inspect", "coding_patch_propose")
    builder.add_edge("coding_patch_propose", "patch_apply")
    builder.add_edge("patch_apply", "verify")
    builder.add_conditional_edges("verify", _route_after_verify)
    builder.add_conditional_edges("coding_patch_repair", _route_after_patch_repair)
    builder.add_edge("reviewer", "synthesizer")
    builder.add_edge("synthesizer", END)
    return builder.compile(checkpointer=checkpointer)


def _observed_node(name: str, deps: GraphDependencies, handler: Any):
    async def node(state: SynodeState) -> SynodeState:
        role = _node_role(name, state)
        await _record_event(
            deps,
            state,
            EventType.NODE_STARTED.value,
            role,
            {"node": name},
        )
        with deps.observability.observation(
            f"node.{name}",
            state.get("observability_trace_id"),
            as_type="agent" if role else "span",
            input_payload={"run_id": state["run_id"], "node": name, "role": role},
            metadata={"mode": state.get("mode"), "model_provider": state.get("model_provider")},
        ):
            try:
                result = await handler(state)
            except (ApprovalRequired, GraphInterrupt):
                raise
            except Exception as exc:
                await _record_event(
                    deps,
                    state,
                    EventType.NODE_COMPLETED.value,
                    role,
                    {"node": name, "ok": False, "error": str(exc)},
                )
                deps.observability.update_current_span(level="ERROR", status_message=str(exc))
                raise
            await _record_event(
                deps,
                state,
                EventType.NODE_COMPLETED.value,
                role,
                {"node": name, "ok": True, "output_keys": sorted(result.keys())},
            )
            deps.observability.update_current_span(output={"ok": True, "output_keys": sorted(result.keys())})
            return result

    return node


def _node_role(name: str, state: SynodeState) -> str | None:
    if name == "supervisor":
        return RoleName.SUPERVISOR.value
    if name == "graph_workers":
        return None
    if name in {"coding_inspect", "coding_patch_propose", "coding_patch_repair", "patch_apply", "verify"}:
        return RoleName.CODER.value
    if name == "reviewer":
        return RoleName.REVIEWER.value
    return None


async def _record_event(
    deps: GraphDependencies,
    state: SynodeState,
    event_type: str,
    role: str | None,
    payload: dict[str, Any],
) -> None:
    async with deps.database.session() as session:
        repo = Repository(session)
        await repo.add_event(state["run_id"], event_type, role, payload)


async def _execute_native_tool(
    deps: GraphDependencies,
    state: SynodeState | dict[str, Any],
    role: str,
    call: ToolCall,
) -> ToolResult:
    result = await deps.tool_executor.execute(state["run_id"], role, state.get("workspace"), call)
    if result.approval_id:
        raise ApprovalRequired(result.approval_id, result.tool_name)
    return result


def _intake_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_event(
                state["run_id"],
                EventType.INTAKE_COMPLETED.value,
                None,
                {"task": state["task"], "mode": state["mode"]},
            )
        return {}

    return node


def _supervisor_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        backend = _role_runtime_backend(state, RoleName.SUPERVISOR.value)
        if backend != "native_langgraph":
            if deps.execution_backends is None:
                raise RuntimeError("execution backend registry is not configured")
            backend_output = await _execute_external_node_with_operator_interrupt(
                deps,
                state,
                backend,
                await _external_node_execution_input(deps, state, RoleName.SUPERVISOR.value),
            )
            if backend_output.status != NodeExecutionStatus.COMPLETED:
                raise RuntimeError(f"external supervisor did not complete: {backend_output.status.value}")
            decision = SupervisorDecision.model_validate(backend_output.payload)
        else:
            provider = await _provider_for_role(deps, state, RoleName.SUPERVISOR.value)
            decision = await _invoke_structured(
                deps,
                state,
                provider,
                SupervisorDecision,
                ModelRequest(
                    role=RoleName.SUPERVISOR.value,
                    prompt=_supervisor_prompt(state, deps),
                    context={
                        "mode": state["mode"],
                        "task": state["task"],
                        "conversation_context": state.get("conversation_context", []),
                    },
                    response_schema=SupervisorDecision,
                    model_options=provider.model_options or {},
                ),
            )
        _validate_supervisor_decision(decision, deps, state)
        if state.get("interaction_mode") == InteractionMode.PLAN_REVIEW.value:
            decision = _supervisor_decision_from_operator_response(
                decision,
                _request_operator(
                    state,
                    kind=OperatorRequestKind.PLAN_REVIEW,
                    prompt="Review the proposed execution plan before Synode starts worker nodes.",
                    context={
                        "task": state["task"],
                        "mode": state["mode"],
                        "workspace": state.get("workspace"),
                    },
                    proposed_payload={"decision": decision.model_dump(mode="json")},
                    node_id=_node_for_role(state, RoleName.SUPERVISOR.value).get("id"),
                    role=RoleName.SUPERVISOR.value,
                ),
            )
            _validate_supervisor_decision(decision, deps, state)
        role_tool_calls = {
            step.role: [call.model_dump(mode="json") for call in step.tool_calls]
            for step in decision.plan
        }
        plan = [
            {"role": step.role, "task": step.task, "tool_calls": role_tool_calls[step.role]}
            for step in decision.plan
        ]
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(
                state["run_id"], "supervisor_decision", decision.model_dump(mode="json")
            )
            for role in decision.selected_roles:
                await repo.add_event(state["run_id"], EventType.ROLE_SELECTED.value, role, {"role": role})
        return {
            "selected_roles": decision.selected_roles,
            "plan": plan,
            "role_tool_calls": role_tool_calls,
            "plan_only": state.get("interaction_mode") == InteractionMode.PLAN_ONLY.value,
        }

    return node


def _route_after_supervisor(state: SynodeState) -> str:
    if state.get("interaction_mode") == InteractionMode.PLAN_ONLY.value or state.get("plan_only"):
        return "synthesizer"
    if state["mode"] == RunMode.CODING.value:
        if _role_runtime_backend(state, RoleName.CODER.value) != "native_langgraph":
            return "graph_workers"
        return "coding_inspect"
    return "graph_workers"


def _route_after_verify(state: SynodeState) -> str:
    verification = state.get("verification_result") or {}
    if (
        state.get("mode") == RunMode.CODING.value
        and state.get("model_provider") != "fake"
        and verification.get("ok") is False
        and not verification.get("skipped")
        and int(state.get("coding_repair_attempts") or 0) < 1
    ):
        return "coding_patch_repair"
    return "reviewer"


def _route_after_patch_repair(state: SynodeState) -> str:
    if state.get("patch_repair_error"):
        return "reviewer"
    return "patch_apply"


def _graph_workers_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        outputs: list[dict[str, Any]] = []
        for role in _topological_worker_order(state, state.get("selected_roles", [])):
            await _record_event(
                deps,
                state,
                EventType.NODE_STARTED.value,
                role,
                {"node": "graph_worker", "role": role},
            )
            output = await _run_worker_role(deps, {**state, "worker_outputs": outputs}, role)
            outputs.append(output.model_dump(mode="json"))
            await _record_event(
                deps,
                state,
                EventType.NODE_COMPLETED.value,
                role,
                {"node": "graph_worker", "role": role, "ok": True},
            )
        return {"worker_outputs": outputs}

    return node


async def _run_worker_role(deps: GraphDependencies, state: SynodeState, role: str) -> AgentOutput:
    backend = _role_runtime_backend(state, role)
    if backend != "native_langgraph":
        if deps.execution_backends is None:
            raise RuntimeError("execution backend registry is not configured")
        backend_output = await _execute_external_node_with_operator_interrupt(
            deps,
            state,
            backend,
            await _external_node_execution_input(deps, state, role),
        )
        return AgentOutput(
            role=backend_output.role,
            summary=backend_output.summary,
            tool_results=backend_output.tool_results,
            risks=backend_output.risks,
        )
    calls = [ToolCall.model_validate(call) for call in state.get("role_tool_calls", {}).get(role, [])]
    results = []
    for call in calls:
        result = await _execute_native_tool(deps, state, role, call)
        results.append(result)
    provider = await _provider_for_role(deps, state, role)
    model_response = await _invoke_model(
        deps,
        state,
        provider,
        ModelRequest(
            role=role,
            prompt=f"Summarize work for task: {state['task']}",
            context={
                "task": state["task"],
                "conversation_context": state.get("conversation_context", []),
                "tool_results": [result.model_dump(mode="json") for result in results],
                "previous_worker_outputs": state.get("worker_outputs", []),
            },
            tools=[call.name for call in calls],
            model_options=provider.model_options or {},
        ),
    )
    output = AgentOutput(
        role=role,
        summary=_summarize_role_output(role, model_response.content, results),
        tool_results=results,
        risks=[result.error for result in results if result.error],
    )
    node = _node_for_role(state, role)
    async with deps.database.session() as session:
        repo = Repository(session)
        await repo.upsert_runtime_node_state(
            state["run_id"],
            str(node.get("id") or role),
            role,
            backend,
            _node_contract_id(state, node),
            NodeExecutionStatus.COMPLETED,
            external_state={"native": True},
        )
    return output


async def _execute_external_node_with_operator_interrupt(
    deps: GraphDependencies,
    state: SynodeState,
    backend: str,
    node_input: NodeExecutionInput,
) -> NodeExecutionOutput:
    if deps.execution_backends is None:
        raise RuntimeError("execution backend registry is not configured")
    output = await deps.execution_backends.execute(backend, node_input)
    for _ in range(3):
        if output.status != NodeExecutionStatus.WAITING_OPERATOR:
            return output
        if output.operator_request is None:
            raise RuntimeError(f"external node requested operator input without a request: {node_input.node_id}")
        request = output.operator_request
        response = _request_operator(
            state,
            kind=request.kind,
            prompt=request.prompt,
            context=request.context,
            proposed_payload=request.proposed_payload,
            node_id=request.node_id or node_input.node_id,
            role=request.role or node_input.role,
        )
        output = await deps.execution_backends.execute(
            backend,
            replace(node_input, operator_response=response),
        )
    raise RuntimeError(f"external node repeatedly requested operator input: {node_input.node_id}")


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
            results.append(await _execute_native_tool(deps, state, RoleName.CODER.value, call))
        provider = await _provider_for_role(deps, state, RoleName.CODER.value)
        inspection = await _invoke_structured(
            deps,
            state,
            provider,
            CodingInspection,
            ModelRequest(
                role=RoleName.CODER.value,
                prompt="Inspect repository evidence and identify files/tests needed for the coding task.",
                context={
                    "task": state["task"],
                    "conversation_context": state.get("conversation_context", []),
                    "tool_results": [result.model_dump(mode="json") for result in results],
                },
                response_schema=CodingInspection,
                model_options=provider.model_options or {},
            ),
        )
        inspection = _augment_inspection_with_search_matches(inspection, results)
        inspection = await _augment_inspection_with_pre_patch_verification(deps, state, inspection)
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(state["run_id"], "coding_inspection", inspection.model_dump(mode="json"))
        return {"coding_inspection": inspection.model_dump(mode="json")}

    return node


def _coding_patch_propose_node(deps: GraphDependencies, *, repair_verification: bool = False):
    async def node(state: SynodeState) -> SynodeState:
        if not repair_verification:
            async with deps.database.session() as session:
                repo = Repository(session)
                existing = await repo.get_latest_artifact(state["run_id"], "patch_proposal")
                if existing is not None:
                    return {"patch_proposal": existing.content}

        file_context = await _read_relevant_files(deps, state)
        provider = await _provider_for_role(deps, state, RoleName.CODER.value)
        context: dict[str, Any] = {
            "task": state["task"],
            "conversation_context": state.get("conversation_context", []),
            "inspection": state.get("coding_inspection", {}),
            "files": file_context,
        }
        if repair_verification:
            current_diff = await _execute_native_tool(
                deps,
                state,
                RoleName.CODER.value,
                ToolCall(name="native.git_diff", arguments={}),
            )
            context.update(
                {
                    "repair_attempt": int(state.get("coding_repair_attempts") or 0) + 1,
                    "previous_patch_proposal": state.get("patch_proposal", {}),
                    "previous_patch_results": state.get("patch_results", []),
                    "failed_verification": state.get("verification_result", {}),
                    "current_diff": _compact_tool_result(current_diff),
                }
            )
        if state["model_provider"] == "fake" or provider.provider.name == "fake":
            context["fake_patch_proposal"] = _fake_patch_proposal(file_context)
        proposal = None
        validation_errors: list[str] = []
        for attempt in range(2):
            proposal_context = dict(context)
            if validation_errors:
                proposal_context["previous_patch_validation_errors"] = validation_errors
                proposal_context["previous_patch_proposal"] = proposal.model_dump(mode="json") if proposal else {}
            proposal = await _invoke_structured(
                deps,
                state,
                provider,
                PatchProposal,
                ModelRequest(
                    role=RoleName.CODER.value,
                    prompt=_patch_proposal_prompt(
                        repair=bool(validation_errors),
                        repair_verification=repair_verification,
                    ),
                    context=proposal_context,
                    response_schema=PatchProposal,
                    model_options=provider.model_options or {},
                ),
            )
            proposal = _normalize_patch_proposal(proposal, file_context)
            validation_errors = _patch_proposal_validation_errors(proposal, file_context)
            if not validation_errors:
                break
        if proposal is None or validation_errors:
            if repair_verification:
                error = f"patch repair proposal validation failed: {validation_errors}"
                async with deps.database.session() as session:
                    repo = Repository(session)
                    await repo.add_artifact(
                        state["run_id"],
                        "patch_repair_error",
                        {
                            "error": error,
                            "validation_errors": validation_errors,
                            "proposal": proposal.model_dump(mode="json") if proposal else None,
                        },
                    )
                return {
                    "coding_repair_attempts": int(state.get("coding_repair_attempts") or 0) + 1,
                    "patch_repair_error": error,
                }
            raise ValueError(f"patch proposal validation failed: {validation_errors}")
        artifact_kind = "patch_repair_proposal" if repair_verification else "patch_proposal"
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(state["run_id"], artifact_kind, proposal.model_dump(mode="json"))
        result: SynodeState = {"patch_proposal": proposal.model_dump(mode="json")}
        if repair_verification:
            result["coding_repair_attempts"] = int(state.get("coding_repair_attempts") or 0) + 1
        return result

    return node


def _patch_apply_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        proposal = PatchProposal.model_validate(state["patch_proposal"])
        result = await _execute_native_tool(
            deps,
            state,
            RoleName.CODER.value,
            ToolCall(
                name="native.patch_apply",
                arguments={"patches": [patch.model_dump(mode="json") for patch in proposal.patches]},
            ),
        )
        results = [result.model_dump(mode="json")]
        if result.ok:
            diff = await _execute_native_tool(
                deps,
                state,
                RoleName.CODER.value,
                ToolCall(name="native.git_diff", arguments={}),
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
        provider = await _provider_for_role(deps, state, RoleName.CODER.value)
        plan = await _invoke_structured(
            deps,
            state,
            provider,
            VerificationPlan,
            ModelRequest(
                role=RoleName.CODER.value,
                prompt="Choose focused verification commands for the applied patch.",
                context={
                    "task": state["task"],
                    "conversation_context": state.get("conversation_context", []),
                    "commands": proposal.verification_commands,
                    "patch_proposal": proposal.model_dump(mode="json"),
                },
                response_schema=VerificationPlan,
                model_options=provider.model_options or {},
            ),
        )
        result = await _execute_native_tool(
            deps,
            state,
            RoleName.CODER.value,
            ToolCall(name="native.verify", arguments={"commands": plan.commands}),
        )
        await _record_event(
            deps,
            state,
            EventType.VERIFICATION_COMPLETED.value,
            RoleName.CODER.value,
            {"ok": result.ok, "error": result.error},
        )
        return {"verification_result": result.model_dump(mode="json")}

    return node


def _reviewer_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        blockers, advisory = _precheck_review_findings(state)
        backend = _role_runtime_backend(state, RoleName.REVIEWER.value)
        if backend != "native_langgraph":
            if deps.execution_backends is None:
                raise RuntimeError("execution backend registry is not configured")
            backend_output = await _execute_external_node_with_operator_interrupt(
                deps,
                state,
                backend,
                await _external_node_execution_input(
                    deps,
                    {
                        **state,
                        "review_precheck": {"blockers": blockers, "advisory": advisory},
                    },
                    RoleName.REVIEWER.value,
                ),
            )
            if backend_output.status != NodeExecutionStatus.COMPLETED:
                raise RuntimeError(f"external reviewer did not complete: {backend_output.status.value}")
            decision = ReviewerDecision.model_validate(backend_output.payload)
        else:
            provider = await _provider_for_role(deps, state, RoleName.REVIEWER.value)
            decision = await _invoke_structured(
                deps,
                state,
                provider,
                ReviewerDecision,
                ModelRequest(
                    role=RoleName.REVIEWER.value,
                    prompt="Review worker outputs, patch results, verification, and policy signals.",
                    context={
                        "blockers": blockers,
                        "advisory": advisory,
                        "conversation_context": state.get("conversation_context", []),
                        "state": _compact_state_for_review(state),
                    },
                    response_schema=ReviewerDecision,
                    model_options=provider.model_options or {},
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
        if state.get("coding_repair_attempts"):
            lines.append(f"\nRepair attempts: {state['coding_repair_attempts']}")
        if state.get("patch_repair_error"):
            lines.append(f"\nPatch repair error: {state['patch_repair_error']}")
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


async def _invoke_structured(
    deps: GraphDependencies,
    state: SynodeState,
    provider: ResolvedModelProvider,
    schema: type[BaseModel],
    request: ModelRequest,
) -> Any:
    response = await _invoke_model(deps, state, provider, request)
    return schema.model_validate(response.structured)


async def _invoke_model(
    deps: GraphDependencies,
    state: SynodeState,
    provider: ResolvedModelProvider,
    request: ModelRequest,
) -> Any:
    with deps.observability.observation(
        f"model.{request.role}",
        state.get("observability_trace_id"),
        as_type="generation",
        input_payload={
            "role": request.role,
            "prompt": request.prompt,
            "context": request.context,
            "tools": request.tools,
        },
        metadata={"run_id": state["run_id"], "response_schema": _schema_name(request.response_schema)},
    ):
        try:
            if _streaming_supported(provider.provider, request):
                response = await _invoke_streaming_model(deps, state, provider, request)
            else:
                response = await provider.provider.invoke(request)
        except Exception as exc:
            await _record_event(
                deps,
                state,
                EventType.MODEL_INVOKED.value,
                request.role,
                {
                    "role": request.role,
                    "ok": False,
                    "profile_id": provider.profile_id,
                    "profile_name": provider.profile_name,
                    "provider_type": provider.provider_type,
                    "error": str(exc),
                },
            )
            deps.observability.update_current_generation(level="ERROR", status_message=str(exc))
            raise
        usage = _response_usage(response)
        await _record_event(
            deps,
            state,
            EventType.MODEL_INVOKED.value,
            request.role,
            {
                "role": request.role,
                "ok": True,
                "profile_id": provider.profile_id,
                "profile_name": provider.profile_name,
                "provider_type": provider.provider_type,
                "provider": response.provider,
                "model": response.model,
                "usage": usage,
                "latency_ms": response.latency_ms,
            },
        )
        deps.observability.update_current_generation(
            output={"content": response.content[:4000], "structured": response.structured},
            model=response.model,
            usage_details={key: value for key, value in usage.items() if isinstance(value, int)},
        )
        return response


async def _invoke_streaming_model(
    deps: GraphDependencies,
    state: SynodeState,
    provider: ResolvedModelProvider,
    request: ModelRequest,
) -> Any:
    stream_id = f"{state['run_id']}:{request.role}:{time.time_ns()}"
    await _record_event(
        deps,
        state,
        EventType.MODEL_STREAM_STARTED.value,
        request.role,
        {
            "stream_id": stream_id,
            "role": request.role,
            "profile_id": provider.profile_id,
            "profile_name": provider.profile_name,
            "provider_type": provider.provider_type,
        },
    )
    pending = ""
    index = 0
    last_flush = time.monotonic()

    async def flush(force: bool = False) -> None:
        nonlocal pending, index, last_flush
        if not pending:
            return
        if not force and len(pending) < 120 and time.monotonic() - last_flush < 0.35:
            return
        index += 1
        await _record_event(
            deps,
            state,
            EventType.MODEL_TOKEN_DELTA.value,
            request.role,
            {
                "stream_id": stream_id,
                "role": request.role,
                "index": index,
                "delta": pending,
            },
        )
        pending = ""
        last_flush = time.monotonic()

    async def on_delta(delta: str) -> None:
        nonlocal pending
        pending += delta
        await flush()

    try:
        invoke_stream = getattr(provider.provider, "invoke_stream", None)
        if not callable(invoke_stream):
            raise RuntimeError(f"provider {provider.provider.name} advertises streaming without invoke_stream")
        response = await invoke_stream(request, on_delta)
    except Exception:
        await flush(force=True)
        await _record_event(
            deps,
            state,
            EventType.MODEL_STREAM_COMPLETED.value,
            request.role,
            {"stream_id": stream_id, "role": request.role, "ok": False},
        )
        raise
    await flush(force=True)
    await _record_event(
        deps,
        state,
        EventType.MODEL_STREAM_COMPLETED.value,
        request.role,
        {
            "stream_id": stream_id,
            "role": request.role,
            "ok": True,
            "content_length": len(response.content),
        },
    )
    return response


def _streaming_supported(provider: Any, request: ModelRequest) -> bool:
    if request.response_schema is not None:
        return False
    if not bool(getattr(provider, "supports_streaming", False)):
        return False
    invoke_stream = getattr(provider, "invoke_stream", None)
    if not callable(invoke_stream):
        raise RuntimeError(f"provider {provider.name} advertises streaming without invoke_stream")
    return True


def _response_usage(response: Any) -> dict[str, int | None]:
    return {
        "input_tokens": response.input_tokens,
        "output_tokens": response.output_tokens,
        "total_tokens": response.total_tokens,
    }


def _schema_name(schema: type[BaseModel] | None) -> str | None:
    return schema.__name__ if schema is not None else None


def _request_operator(
    state: SynodeState | dict[str, Any],
    *,
    kind: OperatorRequestKind | str,
    prompt: str,
    context: dict[str, Any],
    proposed_payload: dict[str, Any],
    node_id: str | None,
    role: str | None,
) -> dict[str, Any]:
    response = interrupt(
        operator_interrupt_payload(
            run_id=str(state["run_id"]),
            thread_id=str(state["thread_id"]),
            kind=kind,
            prompt=prompt,
            context=context,
            proposed_payload=proposed_payload,
            node_id=node_id,
            role=role,
        )
    )
    if not isinstance(response, dict):
        raise ValueError("operator response must be a JSON object")
    return response


def _supervisor_decision_from_operator_response(
    decision: SupervisorDecision,
    response: dict[str, Any],
) -> SupervisorDecision:
    response_type = str(response.get("response_type") or "")
    if response_type == OperatorResponseType.APPROVE.value:
        return decision
    if response_type == OperatorResponseType.EDIT.value:
        payload = response.get("payload")
        if not isinstance(payload, dict):
            raise ValueError("edited plan response requires payload")
        edited = payload.get("decision")
        if not isinstance(edited, dict):
            edited = payload
        return SupervisorDecision.model_validate(edited)
    if response_type == OperatorResponseType.REJECT.value:
        message = response.get("message")
        raise OperatorRejected(str(message) if message else "Operator rejected the execution plan.")
    if response_type == OperatorResponseType.RESPOND.value:
        raise ValueError("plan review requires approve, edit, or reject")
    raise ValueError(f"unknown operator response_type: {response_type}")


def _validate_supervisor_decision(
    decision: SupervisorDecision,
    deps: GraphDependencies,
    state: SynodeState,
) -> None:
    catalog = {role["name"] for role in _worker_role_catalog(deps, state)}
    selected = set(decision.selected_roles)
    planned = {step.role for step in decision.plan}
    if selected != planned:
        raise ValueError(f"plan roles must match selected_roles; missing={selected - planned}, extra={planned - selected}")
    for role in decision.selected_roles:
        if role in {"supervisor", "reviewer"}:
            raise ValueError(f"supervisor selected system role: {role}")
        if role not in catalog:
            raise ValueError(f"supervisor selected role outside active graph: {role}")
        deps.roles.get(role)
    for step in decision.plan:
        if step.role in {"supervisor", "reviewer"}:
            raise ValueError(f"supervisor planned system role: {step.role}")
        if step.role not in catalog:
            raise ValueError(f"supervisor planned role outside active graph: {step.role}")
        deps.roles.get(step.role)
        for call in step.tool_calls:
            deps.tool_executor.tools.get(call.name)
            if not deps.roles.get(step.role).allows_tool(call.name):
                raise PermissionError(f"role '{step.role}' is not allowed to use tool '{call.name}'")


def _supervisor_prompt(state: SynodeState, deps: GraphDependencies) -> str:
    roles = _worker_role_catalog(deps, state)
    return (
        "Create a strict executable plan for Synode.\n"
        f"Mode: {state['mode']}\n"
        f"Task: {state['task']}\n"
        f"Workspace: {state.get('workspace') or '<none>'}\n"
        f"Selectable worker roles: {json.dumps(roles, ensure_ascii=False)}\n"
        "Use conversation_context as background only when it is provided in request context.\n"
        "The current Task is the primary instruction.\n"
        "selected_roles and every plan item MUST use only selectable worker roles.\n"
        "Do not include supervisor or reviewer in selected_roles or plan.\n"
        "tool_calls MUST use only allowed_tools listed on that same role.\n"
        "Do not invent tool names and do not use wildcard names such as mcp.* directly.\n"
        "When a useful tool call is uncertain, use an empty tool_calls list.\n"
        "For native.data_profile, use arguments {} to inspect the first CSV/JSON in the workspace.\n"
        "The set of plan.role values MUST exactly match selected_roles.\n"
        "Return only the requested structured JSON."
    )


def _worker_role_catalog(deps: GraphDependencies, state: SynodeState | dict[str, Any]) -> list[dict[str, Any]]:
    concrete_tools = deps.tool_executor.tools.list_names()
    active_names = set(_graph_worker_names(state))
    roles = []
    for role in deps.roles.as_public():
        role_name = str(role["name"])
        if role_name in {"supervisor", "reviewer"}:
            continue
        if active_names and role_name not in active_names:
            continue
        spec = deps.roles.get(role_name)
        roles.append(
            {
                "name": role["name"],
                "mission": role["mission"],
                "allowed_tools": [tool for tool in concrete_tools if spec.allows_tool(tool)],
            }
        )
    return roles


def _role_runtime_backend(state: SynodeState | dict[str, Any], role: str) -> str:
    node = _node_for_role(state, role)
    backend = node.get("runtime_backend")
    if isinstance(backend, str) and backend:
        return backend
    snapshot = state.get("agent_graph_snapshot") or {}
    node_bindings = snapshot.get("node_runtime_bindings", {})
    if isinstance(node_bindings, dict) and node.get("id") in node_bindings:
        return str(node_bindings[node["id"]])
    return "native_langgraph"


async def _external_node_execution_input(
    deps: GraphDependencies,
    state: SynodeState | dict[str, Any],
    role: str,
) -> NodeExecutionInput:
    node_input = _node_execution_input(state, role)
    if deps.execution_backends is None:
        return node_input
    capabilities = deps.execution_backends.capabilities(node_input.backend_id)
    if not capabilities.supports_tool_proxy:
        return node_input
    proxy = await deps.tool_executor.create_proxy_session(
        run_id=node_input.run_id,
        thread_id=node_input.thread_id,
        node_id=node_input.node_id,
        role_name=node_input.role,
        backend_id=node_input.backend_id,
        workspace=node_input.workspace,
    )
    return replace(
        node_input,
        tool_proxy_url=proxy.url,
        tool_proxy_token=proxy.token,
        tool_proxy_tools=proxy.tools,
    )


def _node_execution_input(state: SynodeState | dict[str, Any], role: str) -> NodeExecutionInput:
    node = _node_for_role(state, role)
    plan_step = _plan_step_for_role(state, role)
    node_id = str(node.get("id") or role)
    backend_id = _role_runtime_backend(state, role)
    contract_id = _node_contract_id(state, node)
    return NodeExecutionInput(
        run_id=state["run_id"],
        thread_id=state["thread_id"],
        node_id=node_id,
        role=role,
        backend_id=backend_id,
        contract_id=contract_id,
        task=state["task"],
        workspace=state.get("workspace"),
        mode=state["mode"],
        conversation_context=state.get("conversation_context", []),
        previous_worker_outputs=state.get("worker_outputs", []),
        upstream_outputs=state.get("worker_outputs", []),
        agent_graph_snapshot=state.get("agent_graph_snapshot", {}),
        role_spec=_role_spec_for_role_with_state(state, role),
        plan_task=str(plan_step.get("task") or state["task"]),
        planned_tool_calls=[
            call
            for call in plan_step.get("tool_calls", [])
            if isinstance(call, dict)
        ],
        model_provider=state.get("model_provider"),
        default_model_profile_id=state.get("default_model_profile_id"),
        role_model_profile_ids=state.get("role_model_profile_ids", {}),
        observability_trace_id=state.get("observability_trace_id"),
    )


def _node_for_role(state: SynodeState | dict[str, Any], role: str) -> dict[str, Any]:
    snapshot = state.get("agent_graph_snapshot") or {}
    nodes = snapshot.get("nodes", [])
    if isinstance(nodes, list):
        for node in nodes:
            if isinstance(node, dict) and node.get("role") == role:
                return node
    return {"id": role, "role": role, "kind": "worker", "contract_id": WORKER_AGENT_OUTPUT_CONTRACT}


def _node_contract_id(state: SynodeState | dict[str, Any], node: dict[str, Any]) -> str:
    contract_id = node.get("contract_id")
    if isinstance(contract_id, str) and contract_id:
        return contract_id
    snapshot = state.get("agent_graph_snapshot") or {}
    contracts = snapshot.get("node_contracts", {})
    node_id = node.get("id")
    if isinstance(contracts, dict) and node_id in contracts:
        return str(contracts[node_id])
    return WORKER_AGENT_OUTPUT_CONTRACT


def _plan_step_for_role(state: SynodeState | dict[str, Any], role: str) -> dict[str, Any]:
    for step in state.get("plan", []):
        if isinstance(step, dict) and step.get("role") == role:
            return step
    return {}


def _role_spec_for_role(state: SynodeState | dict[str, Any], role: str) -> dict[str, Any]:
    snapshot = state.get("agent_graph_snapshot") or {}
    roles = snapshot.get("roles", [])
    if not isinstance(roles, list):
        return {}
    for spec in roles:
        if isinstance(spec, dict) and spec.get("name") == role:
            return spec
    return {}


def _role_spec_for_role_with_state(state: SynodeState | dict[str, Any], role: str) -> dict[str, Any]:
    spec = dict(_role_spec_for_role(state, role))
    review_precheck = state.get("review_precheck")
    if isinstance(review_precheck, dict):
        spec["review_precheck"] = review_precheck
    return spec


async def _provider_for_role(
    deps: GraphDependencies,
    state: SynodeState,
    role: str,
) -> ResolvedModelProvider:
    role_profile_ids = state.get("role_model_profile_ids", {}) or {}
    profile_id = role_profile_ids.get(role) or state.get("default_model_profile_id")
    if not profile_id:
        return ResolvedModelProvider(
            provider=deps.models.get(state["model_provider"]),
            provider_type=state["model_provider"],
        )
    async with deps.database.session() as session:
        repo = Repository(session)
        profile = await repo.get_model_profile(profile_id)
        if profile is None:
            raise LookupError(f"model profile not found: {profile_id}")
        if not profile.enabled:
            raise RuntimeError(f"model profile is disabled: {profile.name}")
        api_key = None
        if profile.secret_id:
            if deps.secret_cipher is None:
                raise RuntimeError("SYNODE_SECRETS_KEY is required for encrypted model profile secrets")
            secret = await repo.get_secret(profile.secret_id)
            if secret is None:
                raise LookupError(f"secret not found: {profile.secret_id}")
            api_key = deps.secret_cipher.decrypt(secret.encrypted_value)
        provider = deps.models.for_profile(profile, api_key)
        options = {
            str(key): value
            for key, value in (profile.options or {}).items()
            if key != "timeout_seconds"
        }
        return ResolvedModelProvider(
            provider=provider,
            profile_id=profile.id,
            profile_name=profile.name,
            provider_type=profile.provider_type,
            model_options=options,
        )


def _graph_worker_names(state: SynodeState | dict[str, Any]) -> list[str]:
    snapshot = state.get("agent_graph_snapshot") or {}
    roles = snapshot.get("roles", [])
    if not isinstance(roles, list):
        return []
    names = []
    for role in roles:
        if not isinstance(role, dict):
            continue
        name = role.get("name")
        if isinstance(name, str) and name not in {"supervisor", "reviewer"}:
            names.append(name)
    return names


def _topological_worker_order(state: SynodeState, selected_roles: list[str]) -> list[str]:
    selected = set(selected_roles)
    if not selected:
        return []
    snapshot = state.get("agent_graph_snapshot") or {}
    node_edges = snapshot.get("node_edges", [])
    nodes = snapshot.get("nodes", [])
    node_by_role = {
        str(node.get("role")): str(node.get("id"))
        for node in nodes
        if isinstance(node, dict) and node.get("role") and node.get("id")
    } if isinstance(nodes, list) else {}
    role_by_node = {node_id: role for role, node_id in node_by_role.items()}
    graph_roles = _graph_worker_names(state)
    if not graph_roles:
        return selected_roles
    selected_node_ids = {node_by_role[role] for role in selected if role in node_by_role}
    order = _topological_order(
        [role for role in graph_roles if role in selected],
        [
            {
                "source": role_by_node[str(edge.get("from_node"))],
                "target": role_by_node[str(edge.get("to_node"))],
            }
            for edge in node_edges
            if isinstance(edge, dict)
            and str(edge.get("from_node")) in selected_node_ids
            and str(edge.get("to_node")) in selected_node_ids
        ],
    )
    missing = [role for role in selected_roles if role not in order]
    return [*order, *missing]


def _topological_order(role_names: list[str], edges: list[dict[str, Any]]) -> list[str]:
    remaining = set(role_names)
    incoming: dict[str, set[str]] = {role: set() for role in role_names}
    outgoing: dict[str, set[str]] = {role: set() for role in role_names}
    for edge in edges:
        source = str(edge.get("source"))
        target = str(edge.get("target"))
        if source in remaining and target in remaining:
            incoming[target].add(source)
            outgoing[source].add(target)
    ordered: list[str] = []
    ready = sorted(role for role, sources in incoming.items() if not sources)
    while ready:
        role = ready.pop(0)
        if role not in remaining:
            continue
        remaining.remove(role)
        ordered.append(role)
        for target in sorted(outgoing[role]):
            incoming[target].discard(role)
            if not incoming[target]:
                ready.append(target)
        ready.sort()
    if remaining:
        raise ValueError(f"agent graph contains a cycle among selected roles: {sorted(remaining)}")
    return ordered


async def _read_relevant_files(deps: GraphDependencies, state: SynodeState) -> list[dict[str, Any]]:
    inspection = CodingInspection.model_validate(state.get("coding_inspection", {}))
    files: list[dict[str, Any]] = []
    for path in _dedupe(inspection.relevant_files)[:5]:
        result = await _execute_native_tool(
            deps,
            state,
            RoleName.CODER.value,
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


def _augment_inspection_with_search_matches(
    inspection: CodingInspection,
    tool_results: list[ToolResult],
) -> CodingInspection:
    relevant_files = list(inspection.relevant_files)
    for result in tool_results:
        if not result.ok:
            continue
        output = result.output or {}
        matches = output.get("matches") if isinstance(output, dict) else None
        if not isinstance(matches, list):
            continue
        for match in matches:
            if not isinstance(match, dict):
                continue
            path = str(match.get("path") or "")
            if _looks_like_test_path(path):
                relevant_files.append(path)
    return inspection.model_copy(update={"relevant_files": _dedupe(relevant_files)})


async def _augment_inspection_with_pre_patch_verification(
    deps: GraphDependencies,
    state: SynodeState,
    inspection: CodingInspection,
) -> CodingInspection:
    commands = inspection.proposed_test_commands[:2]
    if not commands and "pytest" in state["task"].lower():
        commands = [["pytest", "-q"]]
    if not commands:
        return inspection
    result = await _execute_native_tool(
        deps,
        state,
        RoleName.CODER.value,
        ToolCall(name="native.verify", arguments={"commands": commands}),
    )
    if result.ok:
        return inspection
    observed = list(inspection.observed_failures)
    observed.append(_compact_tool_result(result))
    return inspection.model_copy(update={"observed_failures": observed})


def _compact_tool_result(result: ToolResult, limit: int = 3000) -> str:
    payload = result.model_dump(mode="json")
    text = json.dumps(payload, ensure_ascii=False)
    if len(text) > limit:
        return text[:limit].rstrip() + " ...[truncated]"
    return text


def _looks_like_test_path(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path or path.startswith("test_") or "/test_" in path


def _patch_proposal_prompt(*, repair: bool, repair_verification: bool = False) -> str:
    base = (
        "Propose a minimal patch for the coding task using only the provided file contents. "
        "Each patch old_text MUST be a non-empty exact substring from the target file and occur exactly once. "
        "Use complete function or method blocks when changing logic. "
        "new_text MUST be the full replacement for old_text, not a detached snippet. "
        "Respect provided tests, observed_failures, and include focused verification commands. "
        "Patch all root causes shown by the failing tests, not only the first failure. "
        "Do not hard-code fixture-specific customers, names, dates, paths, or expected totals; fix the underlying implementation."
    )
    if repair_verification:
        return (
            base
            + " The previous patch was already applied and verification failed. "
            "Propose a follow-up patch against the CURRENT file contents only. "
            "Use failed_verification and current_diff to repair the implementation, preferably by replacing complete affected functions."
        )
    if repair:
        return (
            base
            + " The previous proposal failed deterministic validation; repair it using previous_patch_validation_errors."
        )
    return base


def _patch_proposal_validation_errors(
    proposal: PatchProposal,
    file_context: list[dict[str, Any]],
) -> list[str]:
    errors: list[str] = []
    content_by_path = {str(item["path"]): str(item["content"]) for item in file_context}
    sha_by_path = {str(item["path"]): str(item["sha256"]) for item in file_context}
    for index, patch in enumerate(proposal.patches):
        content = content_by_path.get(patch.path)
        if content is None:
            errors.append(f"patch {index} targets a file outside provided context: {patch.path}")
            continue
        if patch.expected_sha256 != sha_by_path[patch.path]:
            errors.append(f"patch {index} checksum does not match provided file context: {patch.path}")
        if not patch.old_text:
            errors.append(f"patch {index} old_text is empty: {patch.path}")
            continue
        occurrences = content.count(patch.old_text)
        if occurrences != 1:
            errors.append(f"patch {index} old_text must occur exactly once in {patch.path}; occurrences={occurrences}")
        if patch.old_text == patch.new_text:
            errors.append(f"patch {index} new_text is identical to old_text: {patch.path}")
    for index, command in enumerate(proposal.verification_commands):
        argv = [str(part) for part in command]
        if not argv:
            errors.append(f"verification command {index} is empty")
        elif not is_safe_command(argv):
            errors.append(f"verification command {index} is unsafe: {argv}")
    return errors


def _normalize_patch_proposal(
    proposal: PatchProposal,
    file_context: list[dict[str, Any]],
) -> PatchProposal:
    content_by_path = {str(item["path"]): str(item["content"]) for item in file_context}
    sha_by_path = {str(item["path"]): str(item["sha256"]) for item in file_context}
    patches: list[FilePatch] = []
    for patch in proposal.patches:
        content = content_by_path.get(patch.path)
        old_text = patch.old_text
        if content is not None and content.count(old_text) != 1:
            old_text = _align_old_text_to_content(content, old_text)
        patches.append(
            FilePatch(
                path=patch.path,
                expected_sha256=sha_by_path.get(patch.path, patch.expected_sha256),
                old_text=old_text,
                new_text=patch.new_text,
            )
        )
    return proposal.model_copy(update={"patches": patches})


def _align_old_text_to_content(content: str, old_text: str) -> str:
    simple_candidates = [
        old_text.strip("\n"),
        old_text.strip(),
        old_text.replace("\r\n", "\n"),
        old_text.replace("\r\n", "\n").strip("\n"),
        old_text.replace("\r\n", "\n").strip(),
    ]
    for candidate in simple_candidates:
        if candidate and content.count(candidate) == 1:
            return candidate

    stripped_lines = [line.strip() for line in old_text.strip().splitlines()]
    if not stripped_lines or any(not line for line in stripped_lines):
        return old_text
    content_lines = content.splitlines(keepends=True)
    matches: list[str] = []
    width = len(stripped_lines)
    for start in range(0, len(content_lines) - width + 1):
        window = content_lines[start : start + width]
        if [line.strip() for line in window] != stripped_lines:
            continue
        candidate = "".join(window)
        if candidate:
            matches.append(candidate)
        trimmed = candidate.rstrip("\n")
        if trimmed and trimmed != candidate:
            matches.append(trimmed)
    exact_matches = list(dict.fromkeys(candidate for candidate in matches if content.count(candidate) == 1))
    if len(exact_matches) == 1:
        return exact_matches[0]
    equivalent_matches = {candidate.rstrip("\n") for candidate in exact_matches}
    if len(equivalent_matches) == 1:
        return next(iter(equivalent_matches))
    return old_text


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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
    if state.get("patch_repair_error"):
        blockers.append(str(state["patch_repair_error"]))
    return blockers, advisory


def _classify_tool_result(result: dict[str, Any], blockers: list[str], advisory: list[str]) -> None:
    if result.get("approval_id"):
        blockers.append(f"Approval required for {result['tool_name']}: {result['approval_id']}")
    elif not result.get("ok"):
        blockers.append(f"{result.get('tool_name')} failed: {result.get('error')}")


def _compact_state_for_review(state: SynodeState) -> dict[str, Any]:
    return {
        "mode": state.get("mode"),
        "conversation_context": state.get("conversation_context", []),
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
