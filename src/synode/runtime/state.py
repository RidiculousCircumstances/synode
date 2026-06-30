from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class SynodeState(TypedDict, total=False):
    run_id: str
    task: str
    workspace: str | None
    model_provider: str
    mode: str
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
