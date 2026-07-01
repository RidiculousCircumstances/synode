from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, replace
from typing import Any, cast

from langgraph.constants import END, START
from langgraph.errors import GraphInterrupt
from langgraph.graph import StateGraph
from langgraph.types import interrupt
from pydantic import BaseModel, ValidationError

from synode.models.errors import StructuredOutputValidationError
from synode.models.provider import ModelProviderRegistry, ModelRequest
from synode.observability import Observability
from synode.persistence.database import Database
from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.runtime.contracts import (
    CODING_INSPECTION_CONTRACT,
    CODING_PATCH_PROPOSAL_CONTRACT,
    WORKER_AGENT_OUTPUT_CONTRACT,
    default_contract_registry,
)
from synode.runtime.decisions import (
    CodingInspection,
    FilePatch,
    NativeLoopAction,
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
from synode.validation.operator import invalid_operator_question_text_reason
from synode.validation.patches import (
    categorize_patch_validation_failure,
    dedupe_file_patches,
    extract_required_patch_symbols,
    normalize_patch_proposal,
    required_patch_targets,
    validate_patch_proposal,
)


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


@dataclass(frozen=True)
class NativeLoopResult:
    payload: dict[str, Any]
    tool_results: list[ToolResult]
    trace: list[dict[str, Any]]


class NativeLoopContractError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        trace: list[dict[str, Any]] | None = None,
        tool_results: list[ToolResult] | None = None,
    ) -> None:
        super().__init__(message)
        self.trace = list(trace or [])
        self.tool_results = list(tool_results or [])


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
    builder.add_conditional_edges("coding_inspect", _route_after_coding_inspect)
    builder.add_conditional_edges("coding_patch_propose", _route_after_patch_propose)
    builder.add_edge("patch_apply", "verify")
    builder.add_conditional_edges(
        "verify",
        lambda state: _route_after_verify(
            state,
            repair_attempts=int(deps.tool_executor.settings.coding_repair_attempts),
        ),
    )
    builder.add_conditional_edges("coding_patch_repair", _route_after_patch_repair)
    builder.add_edge("reviewer", "synthesizer")
    builder.add_edge("synthesizer", END)
    return builder.compile(checkpointer=checkpointer)


async def resume_native_coding_after_patch_approval(
    deps: GraphDependencies,
    state: SynodeState | dict[str, Any],
) -> dict[str, Any]:
    resumed_state: dict[str, Any] = {
        **state,
        **await _hydrate_coding_state_from_artifacts(deps, str(state["run_id"])),
    }
    patch_result = await _observed_node("patch_apply", deps, _patch_apply_node(deps))(resumed_state)
    resumed_state.update(patch_result)
    verification_result = await _observed_node("verify", deps, _verify_node(deps))(resumed_state)
    resumed_state.update(verification_result)

    repair_limit = max(0, int(deps.tool_executor.settings.coding_repair_attempts))
    while _route_after_verify(cast(SynodeState, resumed_state), repair_attempts=repair_limit) == "coding_patch_repair":
        repair_result = await _observed_node(
            "coding_patch_repair",
            deps,
            _coding_patch_propose_node(deps, repair_verification=True),
        )(resumed_state)
        resumed_state.update(repair_result)
        if _route_after_patch_repair(cast(SynodeState, resumed_state)) == "reviewer":
            break
        patch_result = await _observed_node("patch_apply", deps, _patch_apply_node(deps))(resumed_state)
        resumed_state.update(patch_result)
        verification_result = await _observed_node("verify", deps, _verify_node(deps))(resumed_state)
        resumed_state.update(verification_result)

    review_result = await _observed_node("reviewer", deps, _reviewer_node(deps))(resumed_state)
    resumed_state.update(review_result)
    synthesis_result = await _observed_node("synthesizer", deps, _synthesizer_node(deps))(resumed_state)
    resumed_state.update(synthesis_result)
    return resumed_state


async def _hydrate_coding_state_from_artifacts(deps: GraphDependencies, run_id: str) -> dict[str, Any]:
    async with deps.database.session() as session:
        repo = Repository(session)
        supervisor = await repo.get_latest_artifact(run_id, "supervisor_decision")
        inspection = await repo.get_latest_artifact(run_id, "coding_inspection")
        context_packet = await repo.get_latest_artifact(run_id, "coding_context_packet")
        candidates = await repo.get_latest_artifact(run_id, "patch_candidates")
        repair_proposal = await repo.get_latest_artifact(run_id, "patch_repair_proposal")
        proposal_artifact = repair_proposal or await repo.get_latest_artifact(run_id, "patch_proposal")

    hydrated: dict[str, Any] = {"worker_outputs": []}
    if supervisor is not None:
        decision = SupervisorDecision.model_validate(supervisor.content)
        role_tool_calls = {
            step.role: [call.model_dump(mode="json") for call in step.tool_calls]
            for step in decision.plan
        }
        hydrated.update(
            {
                "selected_roles": decision.selected_roles,
                "role_tool_calls": role_tool_calls,
                "plan": [
                    {"role": step.role, "task": step.task, "tool_calls": role_tool_calls[step.role]}
                    for step in decision.plan
                ],
            }
        )
    if inspection is not None:
        hydrated["coding_inspection"] = inspection.content
    if context_packet is not None:
        hydrated["coding_context_packet"] = context_packet.content
    if candidates is not None:
        hydrated["patch_candidates"] = candidates.content.get("candidates", [])
    if proposal_artifact is None:
        raise RuntimeError("cannot resume approved patch: patch_proposal artifact is missing")
    proposal = PatchProposal.model_validate(proposal_artifact.content)
    hydrated["patch_proposal"] = proposal.model_dump(mode="json")
    hydrated["coding_action"] = proposal.action
    if proposal_artifact.kind == "patch_repair_proposal":
        hydrated["coding_repair_attempts"] = 1
    return hydrated


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


def _route_after_coding_inspect(state: SynodeState) -> str:
    if state.get("patch_repair_error") or state.get("coding_failure_category"):
        return "reviewer"
    return "coding_patch_propose"


def _route_after_verify(state: SynodeState, *, repair_attempts: int = 2) -> str:
    verification = state.get("verification_result") or {}
    if (
        state.get("mode") == RunMode.CODING.value
        and state.get("model_provider") != "fake"
        and verification.get("ok") is False
        and not verification.get("skipped")
        and int(state.get("coding_repair_attempts") or 0) < max(0, repair_attempts)
    ):
        return "coding_patch_repair"
    return "reviewer"


def _route_after_patch_propose(state: SynodeState) -> str:
    if state.get("patch_repair_error") or state.get("coding_failure_category") == "needs_operator":
        return "reviewer"
    if state.get("coding_action") == "no_change":
        return "verify"
    return "patch_apply"


def _route_after_patch_repair(state: SynodeState) -> str:
    if state.get("patch_repair_error"):
        return "reviewer"
    if state.get("coding_action") == "no_change":
        return "verify"
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
    node_input = _node_execution_input(state, role)
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
    try:
        loop_result = await _run_native_loop(
            deps,
            state,
            role=role,
            node_id=node_input.node_id,
            contract_id=node_input.contract_id,
            prompt=f"Complete worker node for task: {node_input.plan_task or state['task']}",
            context={},
            plan_task=node_input.plan_task,
            planned_tool_calls=node_input.planned_tool_calls,
        )
    except NativeLoopContractError as exc:
        return AgentOutput(
            role=role,
            summary=f"Worker failed to satisfy contract: {exc}",
            tool_results=[],
            risks=[str(exc)],
        )
    if node_input.contract_id == WORKER_AGENT_OUTPUT_CONTRACT:
        payload_output = AgentOutput.model_validate(loop_result.payload)
        raw_summary = payload_output.summary
        raw_risks = payload_output.risks
    else:
        raw_summary = json.dumps(loop_result.payload, ensure_ascii=False)
        raw_risks = []
    output = AgentOutput(
        role=role,
        summary=_summarize_role_output(role, raw_summary, loop_result.tool_results),
        tool_results=loop_result.tool_results,
        risks=[*raw_risks, *[result.error for result in loop_result.tool_results if result.error]],
    )
    async with deps.database.session() as session:
        repo = Repository(session)
        await repo.upsert_runtime_node_state(
            state["run_id"],
            node_input.node_id,
            role,
            backend,
            node_input.contract_id,
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


async def _run_native_loop(
    deps: GraphDependencies,
    state: SynodeState,
    *,
    role: str,
    node_id: str,
    contract_id: str,
    prompt: str,
    context: dict[str, Any],
    plan_task: str | None = None,
    planned_tool_calls: list[dict[str, Any]] | None = None,
) -> NativeLoopResult:
    provider = await _provider_for_role(deps, state, role)
    contract = default_contract_registry().get(contract_id)
    allowed_tools = deps.tool_executor.allowed_tool_names(role)
    tool_catalog = deps.tool_executor.tool_catalog(role)
    max_steps = max(1, int(deps.tool_executor.settings.native_loop_max_steps))
    tool_results: list[ToolResult] = []
    trace: list[dict[str, Any]] = []
    validation_errors: list[str] = []
    seen_tool_calls: dict[str, int] = {}
    base_context = _trim_loop_context(
        {
            "task": state["task"],
            "node_task": plan_task or state["task"],
            "role": role,
            "node_id": node_id,
            "contract_id": contract_id,
            "contract_json_schema": contract.payload_schema.model_json_schema(),
            "workspace": state.get("workspace"),
            "conversation_context": state.get("conversation_context", [])[-4:],
            "previous_worker_outputs": state.get("worker_outputs", []),
            "upstream_outputs": state.get("worker_outputs", []),
            "role_spec": _role_spec_for_role_with_state(state, role),
            "allowed_tools": allowed_tools,
            "tool_catalog": tool_catalog,
            "planned_tool_calls": planned_tool_calls or [],
            **context,
        },
        max_bytes=max(4000, int(deps.tool_executor.settings.native_loop_context_max_bytes)),
    )

    for step_index in range(max_steps):
        request_context = {
            **base_context,
            "loop_step": step_index + 1,
            "remaining_steps": max_steps - step_index,
            "loop_history": trace,
            "previous_validation_errors": validation_errors,
        }
        try:
            action = await _invoke_structured(
                deps,
                state,
                provider,
                NativeLoopAction,
                ModelRequest(
                    role=role,
                    prompt=_native_loop_prompt(prompt, contract_id=contract_id),
                    context=request_context,
                    response_schema=NativeLoopAction,
                    model_options=provider.model_options or {},
                ),
            )
        except (StructuredOutputValidationError, ValidationError) as exc:
            validation_errors = [f"native loop action validation failed: {exc}"]
            trace.append(
                {
                    "step": step_index + 1,
                    "action": "invalid_action",
                    "error": validation_errors[0],
                }
            )
            continue

        if action.action == "tool_call":
            call = action.tool_call
            if call is None:
                validation_errors = ["tool_call action did not include tool_call"]
                trace.append(
                    {
                        "step": step_index + 1,
                        "action": action.action,
                        "summary": action.summary,
                        "error": validation_errors[0],
                    }
                )
                continue
            if call.name not in allowed_tools:
                validation_errors = [f"tool is not allowed for role {role}: {call.name}"]
                trace.append(
                    {
                        "step": step_index + 1,
                        "action": action.action,
                        "summary": action.summary,
                        "tool_call": call.model_dump(mode="json"),
                        "error": validation_errors[0],
                    }
                )
                continue
            call_signature = _tool_call_signature(call)
            repeat_count = seen_tool_calls.get(call_signature, 0)
            if repeat_count == 1:
                seen_tool_calls[call_signature] = 2
                validation_errors = [
                    "duplicate tool call repeated without new information: "
                    f"{call.name} with the same arguments. Use the prior observation from loop_history, "
                    "call a different tool, or finish with the contract payload."
                ]
                trace.append(
                    {
                        "step": step_index + 1,
                        "action": action.action,
                        "summary": action.summary,
                        "tool_call": call.model_dump(mode="json"),
                        "error": validation_errors[0],
                    }
                )
                continue
            if repeat_count >= 2:
                error = f"native loop repeated duplicate tool call after feedback: {call.name}"
                trace.append(
                    {
                        "step": step_index + 1,
                        "action": action.action,
                        "summary": action.summary,
                        "tool_call": call.model_dump(mode="json"),
                        "error": error,
                    }
                )
                await _persist_native_loop_trace(
                    deps,
                    state,
                    role=role,
                    node_id=node_id,
                    contract_id=contract_id,
                    status="failed",
                    trace=trace,
                    error=error,
                )
                raise NativeLoopContractError(error, trace=trace, tool_results=tool_results)
            seen_tool_calls[call_signature] = 1
            try:
                result = await _execute_native_tool(deps, state, role, call)
            except ApprovalRequired as exc:
                trace.append(
                    {
                        "step": step_index + 1,
                        "action": action.action,
                        "summary": action.summary,
                        "tool_call": call.model_dump(mode="json"),
                        "approval_id": exc.approval_id,
                        "error": "approval required",
                    }
                )
                await _persist_native_loop_trace(
                    deps,
                    state,
                    role=role,
                    node_id=node_id,
                    contract_id=contract_id,
                    status="waiting_approval",
                    trace=trace,
                )
                raise
            tool_results.append(result)
            validation_errors = []
            trace.append(
                {
                    "step": step_index + 1,
                    "action": action.action,
                    "summary": action.summary,
                    "tool_call": call.model_dump(mode="json"),
                    "observation": _compact_tool_result(
                        result,
                        limit=max(1000, int(deps.tool_executor.settings.native_loop_observation_max_bytes)),
                    ),
                }
            )
            continue

        if action.action == "needs_operator":
            operator_error = _invalid_operator_request_reason(action, contract_id=contract_id)
            if operator_error:
                validation_errors = [operator_error]
                trace.append(
                    {
                        "step": step_index + 1,
                        "action": action.action,
                        "summary": action.summary,
                        "operator_question": action.operator_question,
                        "error": operator_error,
                    }
                )
                continue
            response = _request_operator(
                state,
                kind=OperatorRequestKind.AMBIGUITY,
                prompt=action.operator_question or action.summary,
                context={
                    "task": state["task"],
                    "node_id": node_id,
                    "role": role,
                    "contract_id": contract_id,
                    "loop_history": trace,
                },
                proposed_payload={"action": action.model_dump(mode="json")},
                node_id=node_id,
                role=role,
            )
            if str(response.get("response_type") or "") == OperatorResponseType.REJECT.value:
                raise OperatorRejected(str(response.get("message") or "operator rejected native loop request"))
            validation_errors = []
            trace.append(
                {
                    "step": step_index + 1,
                    "action": action.action,
                    "summary": action.summary,
                    "operator_question": action.operator_question,
                    "operator_response": response,
                }
            )
            continue

        try:
            validated = default_contract_registry().validate_payload(contract_id, action.payload)
        except ValidationError as exc:
            validation_errors = [f"final payload does not match contract {contract_id}: {exc}"]
            trace.append(
                {
                    "step": step_index + 1,
                    "action": action.action,
                    "summary": action.summary,
                    "payload": action.payload,
                    "error": validation_errors[0],
                }
            )
            continue
        payload = validated.model_dump(mode="json")
        trace.append(
            {
                "step": step_index + 1,
                "action": action.action,
                "summary": action.summary,
                "payload": payload,
            }
        )
        await _persist_native_loop_trace(
            deps,
            state,
            role=role,
            node_id=node_id,
            contract_id=contract_id,
            status="completed",
            trace=trace,
        )
        return NativeLoopResult(payload=payload, tool_results=tool_results, trace=trace)

    error = f"native loop exceeded {max_steps} steps without valid {contract_id} payload"
    await _persist_native_loop_trace(
        deps,
        state,
        role=role,
        node_id=node_id,
        contract_id=contract_id,
        status="failed",
        trace=trace,
        error=error,
    )
    raise NativeLoopContractError(error, trace=trace, tool_results=tool_results)


def _invalid_operator_request_reason(action: NativeLoopAction, *, contract_id: str) -> str | None:
    text = str(action.operator_question or action.summary or "").strip()
    return invalid_operator_question_text_reason(text, contract_id=contract_id)


def _native_loop_prompt(prompt: str, *, contract_id: str) -> str:
    return (
        "Execute this Synode native worker node as a bounded action/observation loop.\n"
        f"Node objective: {prompt}\n"
        f"Final contract: {contract_id}\n"
        "Choose exactly one action: tool_call, needs_operator, or finish.\n"
        "Use only allowed_tools for tool_call. Do not invent tool names.\n"
        "Before every tool_call, read tool_catalog and use the exact input schema.\n"
        "Do not pass unknown arguments. Do not pass workspace root/cwd/path unless the schema asks for path.\n"
        "Use native.fs_list to list files. Use native.fs_search only for regex text search inside files.\n"
        'For tool_call return {"action":"tool_call","summary":"why","tool_call":{"name":"native.fs_list","arguments":{"glob":"*.py"}},"payload":{}}.\n'
        "Do not put tool arguments directly in payload for tool_call unless you cannot emit tool_call.name.\n"
        "Do not repeat an identical tool_call after its observation appears in loop_history.\n"
        "Use needs_operator when the task is ambiguous or blocked by missing operator intent. "
        "Do not use needs_operator to ask the operator to do your implementation, review, patching, or verification work. "
        'For needs_operator return {"action":"needs_operator","summary":"why blocked","operator_question":"specific question"}.\n'
        "Use finish only when payload validates against the final contract schema. "
        "Never return finish with only summary; finish MUST include a payload object.\n"
        f"{_native_loop_finish_example(contract_id)}"
        "Return only the requested structured JSON."
    )


def _native_loop_finish_example(contract_id: str) -> str:
    if contract_id == CODING_PATCH_PROPOSAL_CONTRACT:
        return (
            'For coding_patch_proposal finish shape: {"action":"finish","summary":"patch ready",'
            '"payload":{"action":"patch","summary":"minimal fix","patches":[{"path":"file.py",'
            '"expected_sha256":"64_hex_chars","old_text":"exact old block","new_text":"replacement block"}],'
            '"verification_commands":[["pytest","-q"]]}}.\n'
        )
    if contract_id == CODING_INSPECTION_CONTRACT:
        return (
            'For coding_inspection finish shape: {"action":"finish","summary":"inspection complete",'
            '"payload":{"summary":"evidence summary","relevant_files":["file.py"],'
            '"observed_failures":[],"proposed_test_commands":[["pytest","-q"]]}}.\n'
        )
    return 'For finish shape: {"action":"finish","summary":"done","payload":{...contract fields...}}.\n'


def _tool_call_signature(call: ToolCall) -> str:
    return json.dumps(call.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))


async def _persist_native_loop_trace(
    deps: GraphDependencies,
    state: SynodeState | dict[str, Any],
    *,
    role: str,
    node_id: str,
    contract_id: str,
    status: str,
    trace: list[dict[str, Any]],
    error: str | None = None,
) -> None:
    content: dict[str, Any] = {
        "runtime_backend": "native_langgraph",
        "node_id": node_id,
        "role": role,
        "contract_id": contract_id,
        "status": status,
        "steps": trace,
    }
    if error:
        content["error"] = error
    async with deps.database.session() as session:
        repo = Repository(session)
        await repo.add_artifact(str(state["run_id"]), "native_loop_trace", content)
        if error:
            await repo.add_artifact(
                str(state["run_id"]),
                "native_loop_contract_error",
                content,
            )


def _trim_loop_context(context: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    encoded = json.dumps(context, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= max_bytes:
        return context
    trimmed = dict(context)
    for key in ("agent_graph_snapshot", "conversation_context", "previous_worker_outputs", "upstream_outputs"):
        value = trimmed.get(key)
        if isinstance(value, list):
            trimmed[key] = value[-2:]
        elif isinstance(value, dict):
            trimmed[key] = {"truncated": True}
    encoded = json.dumps(trimmed, ensure_ascii=False, default=str).encode("utf-8")
    if len(encoded) <= max_bytes:
        return trimmed
    trimmed["truncated"] = True
    for key, value in list(trimmed.items()):
        if isinstance(value, str) and len(value.encode("utf-8")) > 1000:
            trimmed[key] = value.encode("utf-8")[:1000].decode("utf-8", errors="ignore") + " ...[truncated]"
    return trimmed


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
        try:
            loop_result = await _run_native_loop(
                deps,
                state,
                role=RoleName.CODER.value,
                node_id="coding_inspect",
                contract_id=CODING_INSPECTION_CONTRACT,
                prompt="Inspect repository evidence and identify files/tests needed for the coding task.",
                context={
                    "seed_tool_results": [result.model_dump(mode="json") for result in results],
                },
                plan_task=state["task"],
                planned_tool_calls=[],
            )
        except NativeLoopContractError as exc:
            error = str(exc)
            inspection = _fallback_coding_inspection_from_loop_error(state, error, [*results, *exc.tool_results])
            async with deps.database.session() as session:
                repo = Repository(session)
                await repo.add_artifact(
                    state["run_id"],
                    "coding_inspection_error",
                    {"error": error, "category": "contract_invalid"},
                )
                await repo.add_artifact(state["run_id"], "coding_inspection", inspection.model_dump(mode="json"))
            if inspection.relevant_files:
                inspection = _augment_inspection_with_search_matches(inspection, [*results, *exc.tool_results])
                inspection = await _augment_inspection_with_pre_patch_verification(deps, state, inspection)
                async with deps.database.session() as session:
                    repo = Repository(session)
                    await repo.add_artifact(state["run_id"], "coding_inspection_fallback", inspection.model_dump(mode="json"))
                return {"coding_inspection": inspection.model_dump(mode="json")}
            return {
                "coding_inspection": inspection.model_dump(mode="json"),
                "coding_failure_category": "contract_invalid",
                "patch_repair_error": error,
            }
        inspection = CodingInspection.model_validate(loop_result.payload)
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
                    existing_proposal = PatchProposal.model_validate(existing.content)
                    existing_result: SynodeState = {
                        "patch_proposal": existing.content,
                        "coding_action": existing_proposal.action,
                    }
                    if existing_proposal.action == "no_change":
                        existing_result["patch_results"] = [_no_change_patch_result()]
                    return existing_result

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
        allowed_commands = _verification_command_catalog(state, state.get("coding_inspection", {}))
        context_packet = _build_coding_context_packet(
            deps,
            state,
            file_context=file_context,
            allowed_commands=allowed_commands,
            repair_verification=repair_verification,
            extra_context=context,
        )
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(state["run_id"], "coding_context_packet", context_packet)
        context = {"coding_context_packet": context_packet}
        if state["model_provider"] == "fake" or provider.provider.name == "fake":
            context["fake_patch_proposal"] = _fake_patch_proposal(file_context)
        required_patch_symbols = extract_required_patch_symbols(context_packet, file_context)
        proposal = None
        validation_errors: list[str] = []
        candidates: list[dict[str, Any]] = []
        candidate_models: list[tuple[PatchProposal, list[str], dict[str, Any]]] = []
        max_candidates = max(1, int(deps.tool_executor.settings.coding_patch_candidates))
        for attempt in range(max_candidates):
            proposal_context = dict(context)
            if required_patch_symbols:
                proposal_context["required_patch_symbols"] = required_patch_symbols
                proposal_context["required_patch_targets"] = required_patch_targets(
                    required_patch_symbols,
                    file_context,
                )
            if validation_errors:
                proposal_context["previous_patch_validation_errors"] = validation_errors
                proposal_context["previous_patch_proposal"] = proposal.model_dump(mode="json") if proposal else {}
            try:
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
            except (StructuredOutputValidationError, ValidationError) as exc:
                validation_errors = [f"patch proposal validation failed before deterministic checks: {exc}"]
                candidates.append(
                    {
                        "attempt": attempt + 1,
                        "proposal": None,
                        "validation_errors": validation_errors,
                        "score": {
                            "valid": False,
                            "score": 0,
                            "changed_bytes": 0,
                            "patch_count": 0,
                            "action": "contract_error",
                        },
                    }
                )
                continue
            proposal = normalize_patch_proposal(
                proposal,
                file_context,
                required_patch_symbols=required_patch_symbols,
            )
            validation_errors = validate_patch_proposal(
                proposal,
                file_context,
                allowed_verification_commands=allowed_commands,
                required_patch_symbols=required_patch_symbols,
            )
            candidates.append(
                {
                    "attempt": attempt + 1,
                    "proposal": proposal.model_dump(mode="json"),
                    "validation_errors": validation_errors,
                    "score": _score_patch_proposal(proposal, validation_errors),
                }
            )
            candidate_models.append((proposal, validation_errors, _score_patch_proposal(proposal, validation_errors)))
        if required_patch_symbols and not any(not errors for _, errors, _ in candidate_models):
            focused_candidate = await _focused_required_symbol_patch_candidate(
                deps,
                state,
                provider,
                base_context=context,
                file_context=file_context,
                allowed_commands=allowed_commands,
                required_patch_symbols=required_patch_symbols,
                previous_validation_errors=validation_errors,
                repair_verification=repair_verification,
                attempt=len(candidates) + 1,
            )
            if focused_candidate is not None:
                focused_proposal, focused_errors, focused_score, focused_record = focused_candidate
                candidates.append(focused_record)
                candidate_models.append((focused_proposal, focused_errors, focused_score))
        valid_candidates = [
            (candidate, errors, score)
            for candidate, errors, score in candidate_models
            if not errors
        ]
        if valid_candidates:
            proposal, validation_errors, _score = max(
                valid_candidates,
                key=lambda item: (int(item[2]["score"]), -int(item[2]["changed_bytes"])),
            )
        if proposal is None or validation_errors:
            if repair_verification:
                category = categorize_patch_validation_failure(validation_errors)
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
                    await repo.add_artifact(state["run_id"], "patch_candidates", {"candidates": candidates})
                return {
                    "coding_repair_attempts": int(state.get("coding_repair_attempts") or 0) + 1,
                    "coding_failure_category": category,
                    "patch_candidates": candidates,
                    "patch_repair_error": error,
                }
            category = categorize_patch_validation_failure(validation_errors)
            async with deps.database.session() as session:
                repo = Repository(session)
                await repo.add_artifact(state["run_id"], "patch_candidates", {"candidates": candidates})
                await repo.add_artifact(
                    state["run_id"],
                    "patch_repair_error",
                    {
                        "error": f"patch proposal validation failed [{category}]: {validation_errors}",
                        "validation_errors": validation_errors,
                        "proposal": proposal.model_dump(mode="json") if proposal else None,
                    },
                )
            return {
                "coding_failure_category": category if category != "patch_invalid" else "no_valid_candidate",
                "patch_candidates": candidates,
                "patch_repair_error": f"patch proposal validation failed [{category}]: {validation_errors}",
            }
        if proposal.action == "needs_operator":
            response = _request_operator(
                state,
                kind=OperatorRequestKind.AMBIGUITY,
                prompt=proposal.operator_question or proposal.summary,
                context={
                    "task": state["task"],
                    "workspace": state.get("workspace"),
                    "coding_context_packet": context_packet,
                },
                proposed_payload={"proposal": proposal.model_dump(mode="json")},
                node_id=_node_for_role(state, RoleName.CODER.value).get("id"),
                role=RoleName.CODER.value,
            )
            if str(response.get("response_type") or "") == OperatorResponseType.REJECT.value:
                raise OperatorRejected(str(response.get("message") or "operator rejected coding request"))
            async with deps.database.session() as session:
                repo = Repository(session)
                await repo.add_artifact(
                    state["run_id"],
                    "coding_operator_response",
                    {"proposal": proposal.model_dump(mode="json"), "response": response},
                )
                await repo.add_artifact(state["run_id"], "patch_candidates", {"candidates": candidates})
            return {
                "coding_action": proposal.action,
                "coding_failure_category": "needs_operator",
                "patch_candidates": candidates,
                "patch_repair_error": "coding task needs operator clarification before mutation",
            }
        artifact_kind = "patch_repair_proposal" if repair_verification else "patch_proposal"
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(state["run_id"], artifact_kind, proposal.model_dump(mode="json"))
            await repo.add_artifact(state["run_id"], "patch_candidates", {"candidates": candidates})
        proposal_result: SynodeState = {
            "coding_context_packet": context_packet,
            "coding_action": proposal.action,
            "patch_candidates": candidates,
            "patch_proposal": proposal.model_dump(mode="json"),
        }
        if proposal.action == "no_change":
            proposal_result["patch_results"] = [_no_change_patch_result()]
        if repair_verification:
            proposal_result["coding_repair_attempts"] = int(state.get("coding_repair_attempts") or 0) + 1
        return proposal_result

    return node


async def _focused_required_symbol_patch_candidate(
    deps: GraphDependencies,
    state: SynodeState,
    provider: ResolvedModelProvider,
    *,
    base_context: dict[str, Any],
    file_context: list[dict[str, Any]],
    allowed_commands: list[list[str]],
    required_patch_symbols: list[str],
    previous_validation_errors: list[str],
    repair_verification: bool,
    attempt: int,
) -> tuple[PatchProposal, list[str], dict[str, Any], dict[str, Any]] | None:
    focused_records: list[dict[str, Any]] = []
    patches: list[FilePatch] = []
    verification_commands: list[list[str]] = []
    focused_errors: list[str] = []
    for symbol in required_patch_symbols:
        symbol_errors = list(previous_validation_errors)
        accepted = False
        for focused_attempt in range(2):
            proposal_context = dict(base_context)
            proposal_context.update(
                {
                    "all_required_patch_symbols": required_patch_symbols,
                    "required_patch_symbols": [symbol],
                    "required_patch_targets": required_patch_targets([symbol], file_context),
                    "patch_focus": {
                        "symbol": symbol,
                        "attempt": focused_attempt + 1,
                        "instruction": (
                            f"Patch only the source function {symbol}. "
                            "A proposal that does not modify this function is invalid."
                        ),
                    },
                }
            )
            if symbol_errors:
                proposal_context["previous_patch_validation_errors"] = symbol_errors
            try:
                focused = await _invoke_structured(
                    deps,
                    state,
                    provider,
                    PatchProposal,
                    ModelRequest(
                        role=RoleName.CODER.value,
                        prompt=_patch_proposal_prompt(
                            repair=bool(symbol_errors),
                            repair_verification=repair_verification,
                            focused_symbol=symbol,
                        ),
                        context=proposal_context,
                        response_schema=PatchProposal,
                        model_options=provider.model_options or {},
                    ),
                )
            except (StructuredOutputValidationError, ValidationError) as exc:
                errors = [f"focused patch proposal for {symbol} failed before deterministic checks: {exc}"]
                focused_records.append(
                    {
                        "symbol": symbol,
                        "attempt": focused_attempt + 1,
                        "proposal": None,
                        "validation_errors": errors,
                        "score": {
                            "valid": False,
                            "score": 0,
                            "changed_bytes": 0,
                            "patch_count": 0,
                            "action": "contract_error",
                        },
                    }
                )
                symbol_errors = errors
                continue
            focused = normalize_patch_proposal(
                focused,
                file_context,
                required_patch_symbols=[symbol],
            )
            errors = validate_patch_proposal(
                focused,
                file_context,
                allowed_verification_commands=allowed_commands,
                required_patch_symbols=[symbol],
            )
            focused_records.append(
                {
                    "symbol": symbol,
                    "attempt": focused_attempt + 1,
                    "proposal": focused.model_dump(mode="json"),
                    "validation_errors": errors,
                    "score": _score_patch_proposal(focused, errors),
                }
            )
            if errors:
                symbol_errors = [f"{symbol}: {error}" for error in errors]
                continue
            patches.extend(focused.patches)
            verification_commands.extend(focused.verification_commands)
            accepted = True
            break
        if not accepted:
            focused_errors.extend(symbol_errors)
    if not patches:
        return None
    proposal = PatchProposal(
        action="patch",
        summary="Merged focused patches for required failing source functions.",
        patches=dedupe_file_patches(patches),
        verification_commands=_dedupe_commands(verification_commands) or allowed_commands[:1] or [["pytest", "-q"]],
    )
    proposal = normalize_patch_proposal(
        proposal,
        file_context,
        required_patch_symbols=required_patch_symbols,
    )
    validation_errors = [
        *focused_errors,
        *validate_patch_proposal(
            proposal,
            file_context,
            allowed_verification_commands=allowed_commands,
            required_patch_symbols=required_patch_symbols,
        ),
    ]
    score = _score_patch_proposal(proposal, validation_errors)
    return (
        proposal,
        validation_errors,
        score,
        {
            "attempt": attempt,
            "mode": "focused_required_symbols",
            "proposal": proposal.model_dump(mode="json"),
            "focused": focused_records,
            "validation_errors": validation_errors,
            "score": score,
        },
    )


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
        if state.get("coding_action") != "no_change" and (not patch_results or not patch_results[0].ok):
            return {"verification_result": {"skipped": True, "reason": "patch did not apply"}}
        proposal = PatchProposal.model_validate(state["patch_proposal"])
        commands = _select_verification_commands(proposal, state)
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(
                state["run_id"],
                "verification_plan",
                VerificationPlan(commands=commands, reason="Selected from Synode verification allowlist.").model_dump(
                    mode="json"
                ),
            )
        result = await _execute_native_tool(
            deps,
            state,
            RoleName.CODER.value,
            ToolCall(name="native.verify", arguments={"commands": commands}),
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
        if state.get("coding_failure_category"):
            lines.append(f"\nFailure category: {state['coding_failure_category']}")
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
                    "truncated": bool(result.output.get("truncated")),
                }
            )
    if not files:
        raise FileNotFoundError("coding inspection did not produce readable relevant files")
    return files


def _fallback_coding_inspection_from_loop_error(
    state: SynodeState | dict[str, Any],
    error: str,
    tool_results: list[ToolResult],
) -> CodingInspection:
    relevant_files: list[str] = []
    observed_failures = [error]
    workspace = str(state.get("workspace") or "")
    for result in tool_results:
        output = result.output or {}
        if result.error:
            observed_failures.append(result.error)
        if not result.ok or not isinstance(output, dict):
            continue
        if result.tool_name == "native.fs_read":
            path = _workspace_relative_path(workspace, str(output.get("path") or ""))
            if path:
                relevant_files.append(path)
        matches = output.get("matches")
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, dict):
                    continue
                path = str(match.get("path") or "")
                if path and (path.endswith(".py") or _looks_like_test_path(path)):
                    relevant_files.append(path)
    commands = [["pytest", "-q"]] if "pytest" in str(state.get("task") or "").lower() else []
    return CodingInspection(
        summary=f"Fallback inspection from native loop observations after contract failure: {error}",
        relevant_files=_dedupe(relevant_files)[:5],
        observed_failures=_dedupe(observed_failures),
        proposed_test_commands=commands,
    )


def _workspace_relative_path(workspace: str, path: str) -> str:
    if not path:
        return ""
    if workspace and path.startswith(workspace.rstrip("/") + "/"):
        return path[len(workspace.rstrip("/")) + 1 :]
    if path.startswith("/"):
        return ""
    return path


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


def _verification_command_catalog(
    state: SynodeState | dict[str, Any],
    inspection: dict[str, Any],
) -> list[list[str]]:
    commands: list[list[str]] = []
    proposed = inspection.get("proposed_test_commands", []) if isinstance(inspection, dict) else []
    for command in proposed:
        if isinstance(command, list):
            commands.append([str(part) for part in command])
    task = str(state.get("task") or "").lower()
    relevant_files = inspection.get("relevant_files", []) if isinstance(inspection, dict) else []
    if "pytest" in task or any(_looks_like_test_path(str(path)) for path in relevant_files):
        commands.append(["pytest", "-q"])
        commands.append(["python", "-m", "pytest"])
    if not commands:
        commands.append(["pytest", "-q"])
    return _dedupe_commands([command for command in commands if is_safe_command(command)])


def _select_verification_commands(proposal: PatchProposal, state: SynodeState | dict[str, Any]) -> list[list[str]]:
    packet = state.get("coding_context_packet") or {}
    allowed = packet.get("allowed_verification_commands") if isinstance(packet, dict) else None
    allowed_commands = [
        [str(part) for part in command]
        for command in allowed
        if isinstance(command, list)
    ] if isinstance(allowed, list) else []
    allowed_keys = {_command_key(command) for command in allowed_commands}
    selected = [
        [str(part) for part in command]
        for command in proposal.verification_commands
        if is_safe_command([str(part) for part in command])
        and (not allowed_keys or _command_key([str(part) for part in command]) in allowed_keys)
    ]
    if selected:
        return _dedupe_commands(selected)
    if allowed_commands:
        return allowed_commands[:1]
    return [["pytest", "-q"]]


def _build_coding_context_packet(
    deps: GraphDependencies,
    state: SynodeState,
    *,
    file_context: list[dict[str, Any]],
    allowed_commands: list[list[str]],
    repair_verification: bool,
    extra_context: dict[str, Any],
) -> dict[str, Any]:
    settings = deps.tool_executor.settings
    files = [
        _context_file_window(
            path=str(item["path"]),
            sha256=str(item["sha256"]),
            content=str(item["content"]),
            max_lines=max(20, int(settings.coding_file_window_lines)),
        )
        for item in file_context
    ]
    packet: dict[str, Any] = {
        "version": 1,
        "mode": "repair" if repair_verification else "patch",
        "task": state["task"],
        "conversation_context": state.get("conversation_context", [])[-4:],
        "rules": [
            "Return only structured output matching the schema.",
            "Do not invent file paths; use only files in this packet.",
            "Use exact old_text from the provided current file window.",
            "Choose verification commands only from allowed_verification_commands.",
            "Do not hard-code fixture-specific names, dates, totals, or test literals.",
            "Use action=no_change when the workspace already satisfies the task.",
            "Use action=needs_operator when requirements are ambiguous.",
        ],
        "allowed_verification_commands": allowed_commands,
        "inspection": state.get("coding_inspection", {}),
        "files": files,
    }
    if repair_verification:
        packet["previous_patch_proposal"] = extra_context.get("previous_patch_proposal", {})
        packet["previous_patch_results"] = extra_context.get("previous_patch_results", [])
        packet["failed_verification"] = extra_context.get("failed_verification", {})
        packet["current_diff"] = extra_context.get("current_diff", {})
        packet["repair_attempt"] = extra_context.get("repair_attempt")
    return _trim_context_packet(packet, max_bytes=max(4000, int(settings.coding_context_max_bytes)))


def _context_file_window(*, path: str, sha256: str, content: str, max_lines: int) -> dict[str, Any]:
    lines = content.splitlines()
    truncated = len(lines) > max_lines
    visible = lines[:max_lines]
    return {
        "path": path,
        "sha256": sha256,
        "line_start": 1,
        "line_end": len(visible),
        "truncated": truncated,
        "content": "\n".join(visible) + ("\n" if content.endswith("\n") and visible else ""),
    }


def _trim_context_packet(packet: dict[str, Any], *, max_bytes: int) -> dict[str, Any]:
    encoded = json.dumps(packet, ensure_ascii=False).encode("utf-8")
    if len(encoded) <= max_bytes:
        return packet
    files = packet.get("files", [])
    if not isinstance(files, list) or not files:
        packet["truncated"] = True
        return packet
    budget_per_file = max(500, max_bytes // max(1, len(files)) - 1000)
    trimmed_files = []
    for item in files:
        if not isinstance(item, dict):
            continue
        content = str(item.get("content") or "")
        encoded_content = content.encode("utf-8")
        if len(encoded_content) <= budget_per_file:
            trimmed_files.append(item)
            continue
        shortened = encoded_content[:budget_per_file].decode("utf-8", errors="ignore")
        trimmed = dict(item)
        trimmed["content"] = shortened.rstrip() + "\n...[truncated]"
        trimmed["truncated"] = True
        trimmed_files.append(trimmed)
    packet = dict(packet)
    packet["files"] = trimmed_files
    packet["truncated"] = True
    return packet


def _looks_like_test_path(path: str) -> bool:
    return path.startswith("tests/") or "/tests/" in path or path.startswith("test_") or "/test_" in path


def _patch_proposal_prompt(
    *,
    repair: bool,
    repair_verification: bool = False,
    focused_symbol: str | None = None,
) -> str:
    base = (
        "Use the coding_context_packet as the only repository evidence. "
        "Return one PatchProposal JSON object directly. "
        "Choose exactly one PatchProposal action: patch, no_change, or needs_operator. "
        "For action=patch, propose a minimal patch using only provided file contents. "
        "Each patch old_text MUST be a non-empty exact substring from the target file and occur exactly once. "
        "Use complete function or method blocks when changing logic. "
        "new_text MUST be the full replacement for old_text, not a detached snippet. "
        "verification_commands MUST be copied from allowed_verification_commands. "
        "Patch all root causes shown by the failing tests, not only the first failure. "
        "If required_patch_symbols is present, include a patch inside or replacing every listed source function. "
        "Use required_patch_targets to find those function bodies. "
        "For dictionary accumulators initialized as {}, avoid direct totals[key] +=/-= unless the key is "
        "already initialized; prefer totals.get(key, default) +/- amount. "
        "For refund workflows, do not skip refund rows; make amount negative before the same accumulator update. "
        "Do not hard-code fixture-specific customers, names, dates, paths, or expected totals; fix the underlying implementation. "
        "Use no_change if no mutation is needed. "
        "Use needs_operator only for one concrete ambiguity question ending with '?'. "
        "Do not use needs_operator to ask the operator to review, implement, patch, or run tests."
    )
    if focused_symbol:
        base += (
            f" Focused patch mode: patch only source function {focused_symbol}. "
            f"The patch must modify {focused_symbol}; do not patch other required functions in this focused call."
        )
    if repair_verification:
        return (
            base
            + " The previous patch was already applied and verification failed. "
            "Propose a follow-up patch against the CURRENT file contents only. "
            "Do not repeat previous_patch_proposal or old_text that no longer exists. "
            "Use failed_verification and current_diff to repair the currently failing implementation only, "
            "preferably by replacing complete affected functions."
        )
    if repair:
        return (
            base
            + " The previous proposal failed deterministic validation; repair it using previous_patch_validation_errors."
        )
    return base


def _score_patch_proposal(proposal: PatchProposal, validation_errors: list[str]) -> dict[str, Any]:
    changed_bytes = sum(len(patch.new_text.encode("utf-8")) for patch in proposal.patches)
    score = 100
    score -= len(validation_errors) * 50
    score -= min(30, changed_bytes // 1000)
    if proposal.action == "needs_operator":
        score -= 10
    return {
        "valid": not validation_errors,
        "score": max(0, score),
        "changed_bytes": changed_bytes,
        "patch_count": len(proposal.patches),
        "action": proposal.action,
    }


def _no_change_patch_result() -> dict[str, Any]:
    return {
        "tool_name": "native.patch_apply",
        "ok": True,
        "risk": "read",
        "output": {"skipped": True, "reason": "model selected no_change"},
        "error": None,
        "approval_id": None,
    }


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _dedupe_commands(commands: list[list[str]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    result: list[list[str]] = []
    for command in commands:
        key = _command_key(command)
        if key in seen:
            continue
        seen.add(key)
        result.append(command)
    return result


def _command_key(command: list[str]) -> tuple[str, ...]:
    return tuple(str(part) for part in command)


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
    if state.get("coding_failure_category"):
        blockers.append(f"coding failure category: {state['coding_failure_category']}")
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
