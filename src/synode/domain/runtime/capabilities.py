from __future__ import annotations

from dataclasses import dataclass

from synode.domain.models import RuntimeBackend
from synode.domain.runtime.contracts import (
    CODING_INSPECTION_CONTRACT,
    CODING_PATCH_PROPOSAL_CONTRACT,
    REVIEWER_DECISION_CONTRACT,
    SUPERVISOR_DECISION_CONTRACT,
    TOOL_RESULT_CONTRACT,
    VERIFICATION_PLAN_CONTRACT,
    WORKER_AGENT_OUTPUT_CONTRACT,
)


@dataclass(frozen=True)
class ExecutionBackendCapabilities:
    backend_id: str
    supported_contracts: frozenset[str]
    supports_tool_proxy: bool
    supports_workspace: bool

    def supports_contract(self, contract_id: str) -> bool:
        return contract_id in self.supported_contracts


NATIVE_LANGGRAPH_CAPABILITIES = ExecutionBackendCapabilities(
    backend_id=RuntimeBackend.NATIVE_LANGGRAPH.value,
    supported_contracts=frozenset(
        {
            SUPERVISOR_DECISION_CONTRACT,
            REVIEWER_DECISION_CONTRACT,
            WORKER_AGENT_OUTPUT_CONTRACT,
            CODING_INSPECTION_CONTRACT,
            CODING_PATCH_PROPOSAL_CONTRACT,
            VERIFICATION_PLAN_CONTRACT,
            TOOL_RESULT_CONTRACT,
        }
    ),
    supports_tool_proxy=False,
    supports_workspace=True,
)

OPENHANDS_CAPABILITIES = ExecutionBackendCapabilities(
    backend_id=RuntimeBackend.OPENHANDS.value,
    supported_contracts=frozenset(
        {
            SUPERVISOR_DECISION_CONTRACT,
            REVIEWER_DECISION_CONTRACT,
            WORKER_AGENT_OUTPUT_CONTRACT,
        }
    ),
    supports_tool_proxy=True,
    supports_workspace=True,
)

DEFAULT_BACKEND_CAPABILITIES = {
    NATIVE_LANGGRAPH_CAPABILITIES.backend_id: NATIVE_LANGGRAPH_CAPABILITIES,
    OPENHANDS_CAPABILITIES.backend_id: OPENHANDS_CAPABILITIES,
}


def backend_capabilities(backend_id: str) -> ExecutionBackendCapabilities:
    try:
        return DEFAULT_BACKEND_CAPABILITIES[backend_id]
    except KeyError as exc:
        raise ValueError(f"unknown execution backend: {backend_id}") from exc


def validate_backend_contract(backend_id: str, contract_id: str) -> None:
    capabilities = backend_capabilities(backend_id)
    if not capabilities.supports_contract(contract_id):
        raise ValueError(f"runtime backend {backend_id} does not support node contract {contract_id}")
