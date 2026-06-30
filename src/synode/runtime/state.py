from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class SynodeState(TypedDict, total=False):
    run_id: str
    thread_id: str
    task: str
    conversation_context: list[dict[str, Any]]
    workspace: str | None
    model_provider: str
    default_model_profile_id: str | None
    role_model_profile_ids: dict[str, str]
    agent_graph_id: str | None
    agent_graph_snapshot: dict[str, Any]
    mode: str
    observability_trace_id: str | None
    selected_roles: list[str]
    plan: list[dict[str, Any]]
    role_tool_calls: dict[str, list[dict[str, Any]]]
    current_role: str
    worker_outputs: Annotated[list[dict[str, Any]], operator.add]
    coding_inspection: dict[str, Any]
    patch_proposal: dict[str, Any]
    patch_results: list[dict[str, Any]]
    verification_result: dict[str, Any]
    review: dict[str, Any]
    final_answer: str
