from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from langgraph.constants import END, START
from langgraph.graph import StateGraph
from langgraph.types import Send

from synode.models.provider import ModelProviderRegistry, ModelRequest
from synode.persistence.database import Database
from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.runtime.routing import select_worker_roles
from synode.runtime.state import SynodeState
from synode.schemas import AgentOutput, EventType, PlanStep, RoleName, ToolCall
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
    builder.add_node("reviewer", _reviewer_node(deps))
    builder.add_node("synthesizer", _synthesizer_node(deps))
    builder.add_edge(START, "intake")
    builder.add_edge("intake", "supervisor")
    builder.add_conditional_edges("supervisor", _dispatch_workers, ["worker"])
    builder.add_edge("worker", "reviewer")
    builder.add_edge("reviewer", "synthesizer")
    builder.add_edge("synthesizer", END)
    return builder.compile(checkpointer=checkpointer)


def _intake_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_event(state["run_id"], "intake_completed", None, {"task": state["task"]})
        return {}

    return node


def _supervisor_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        roles = select_worker_roles(state["task"])
        plan = [PlanStep(role=role, task=_role_task(role, state["task"])).model_dump() for role in roles]
        provider = deps.models.get(state["model_provider"])
        await provider.invoke(
            ModelRequest(
                role=RoleName.SUPERVISOR.value,
                prompt=f"Plan task: {state['task']}",
                context={"selected_roles": roles},
            )
        )
        async with deps.database.session() as session:
            repo = Repository(session)
            for role in roles:
                await repo.add_event(state["run_id"], EventType.ROLE_SELECTED.value, role, {"role": role})
        return {"selected_roles": roles, "plan": plan}

    return node


def _dispatch_workers(state: SynodeState) -> list[Send]:
    return [Send("worker", {**state, "current_role": role}) for role in state.get("selected_roles", [])]


def _worker_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        role = state["current_role"]
        calls = _tool_calls_for_role(role, state["task"])
        results = []
        for call in calls:
            result = await deps.tool_executor.execute(
                state["run_id"], role, state.get("workspace"), call
            )
            results.append(result)
        provider = deps.models.get(state["model_provider"])
        model_response = await provider.invoke(
            ModelRequest(
                role=role,
                prompt=f"Summarize work for task: {state['task']}",
                context={"tool_results": [result.model_dump() for result in results]},
                tools=[call.name for call in calls],
            )
        )
        risks = [result.error for result in results if result.error]
        output = AgentOutput(
            role=role,
            summary=_summarize_role_output(role, model_response.content, results),
            tool_results=results,
            risks=[risk for risk in risks if risk],
        )
        return {"worker_outputs": [output.model_dump()]}

    return node


def _reviewer_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        outputs = state.get("worker_outputs", [])
        blockers: list[str] = []
        advisory: list[str] = []
        for output in outputs:
            for result in output.get("tool_results", []):
                if result.get("approval_id"):
                    blockers.append(f"Approval required for {result['tool_name']}: {result['approval_id']}")
                elif not result.get("ok"):
                    advisory.append(f"{output['role']} tool failed: {result.get('error')}")
        provider = deps.models.get(state["model_provider"])
        await provider.invoke(
            ModelRequest(
                role=RoleName.REVIEWER.value,
                prompt="Review worker outputs for blockers and risks.",
                context={"blockers": blockers, "advisory": advisory},
            )
        )
        return {"review": {"blockers": blockers, "advisory": advisory, "can_proceed": not blockers}}

    return node


def _synthesizer_node(deps: GraphDependencies):
    async def node(state: SynodeState) -> SynodeState:
        lines = ["Synode run summary:"]
        for step in state.get("plan", []):
            lines.append(f"- {step['role']}: {step['task']}")
        for output in state.get("worker_outputs", []):
            lines.append(f"\n[{output['role']}]\n{output['summary']}")
        review = state.get("review", {})
        if review.get("blockers"):
            lines.append("\nBlockers:")
            lines.extend(f"- {item}" for item in review["blockers"])
        if review.get("advisory"):
            lines.append("\nAdvisory risks:")
            lines.extend(f"- {item}" for item in review["advisory"])
        final = "\n".join(lines)
        async with deps.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(state["run_id"], "final_answer", {"text": final})
        return {"final_answer": final}

    return node


def _role_task(role: str, task: str) -> str:
    if role == RoleName.CODER.value:
        return "Inspect repository files and safe local status."
    if role == RoleName.DATA_ANALYST.value:
        return "Profile local CSV/JSON data and summarize numeric signals."
    if role == RoleName.WEB_RESEARCHER.value:
        return "Search or fetch public web sources relevant to the task."
    if role == RoleName.DB_AGENT.value:
        return "Inspect database schema or execute bounded read-only SQL."
    return task


def _tool_calls_for_role(role: str, task: str) -> list[ToolCall]:
    if role == RoleName.CODER.value:
        return [
            ToolCall(name="native.fs_search", arguments={"pattern": "TODO|FIXME|error|raise", "glob": "*.py", "max_matches": 20}),
            ToolCall(name="native.shell", arguments={"argv": ["git", "status", "--short"]}),
        ]
    if role == RoleName.DATA_ANALYST.value:
        path = _first_path_with_suffix(task, {".csv", ".json"})
        return [ToolCall(name="native.data_profile", arguments={"path": path} if path else {})]
    if role == RoleName.WEB_RESEARCHER.value:
        url = _first_url(task)
        if url:
            return [ToolCall(name="native.web_fetch", arguments={"url": url})]
        return [ToolCall(name="native.web_search", arguments={"query": task, "limit": 5})]
    if role == RoleName.DB_AGENT.value:
        sql = _extract_sql(task)
        return [ToolCall(name="native.db_readonly", arguments={"sql": sql} if sql else {})]
    return []


def _summarize_role_output(role: str, model_content: str, results: list[Any]) -> str:
    lines = [model_content]
    for result in results:
        if result.ok:
            lines.append(f"- {result.tool_name}: ok {result.output}")
        else:
            lines.append(f"- {result.tool_name}: failed {result.error}")
    return "\n".join(lines)


def _first_path_with_suffix(task: str, suffixes: set[str]) -> str | None:
    for token in re.findall(r"[\w./-]+", task):
        lower = token.lower()
        if any(lower.endswith(suffix) for suffix in suffixes):
            return token
    return None


def _first_url(task: str) -> str | None:
    match = re.search(r"https?://\S+", task)
    return match.group(0).rstrip(".,)") if match else None


def _extract_sql(task: str) -> str | None:
    match = re.search(r"sql\s*:\s*(.+)", task, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None

