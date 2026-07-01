from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from synode.domain.models import OperatorRequestKind


@dataclass(frozen=True)
class OperatorInterrupt:
    interrupt_id: str
    payload: dict[str, Any]


class OperatorRejected(RuntimeError):
    pass


class ApprovalRequired(RuntimeError):
    def __init__(self, approval_id: str, tool_name: str):
        super().__init__(f"approval required for {tool_name}: {approval_id}")
        self.approval_id = approval_id
        self.tool_name = tool_name


def operator_interrupt_payload(
    *,
    run_id: str,
    thread_id: str,
    kind: OperatorRequestKind | str,
    prompt: str,
    context: dict[str, Any] | None = None,
    proposed_payload: dict[str, Any] | None = None,
    node_id: str | None = None,
    role: str | None = None,
) -> dict[str, Any]:
    kind_value = kind.value if isinstance(kind, OperatorRequestKind) else str(kind)
    return {
        "type": "operator_request",
        "version": 1,
        "run_id": run_id,
        "thread_id": thread_id,
        "node_id": node_id,
        "role": role,
        "kind": kind_value,
        "prompt": prompt,
        "context": context or {},
        "proposed_payload": proposed_payload or {},
    }
