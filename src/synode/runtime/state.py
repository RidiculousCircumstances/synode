from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class SynodeState(TypedDict, total=False):
    run_id: str
    task: str
    workspace: str | None
    model_provider: str
    selected_roles: list[str]
    plan: list[dict[str, Any]]
    current_role: str
    worker_outputs: Annotated[list[dict[str, Any]], operator.add]
    review: dict[str, Any]
    final_answer: str

