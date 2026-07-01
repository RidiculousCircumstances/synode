from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from synode.runtime.decisions import (
    CodingInspection,
    PatchProposal,
    ReviewerDecision,
    SupervisorDecision,
    VerificationPlan,
)
from synode.schemas import AgentGraphNodeKind, AgentOutput, RoleName, ToolResult

SUPERVISOR_DECISION_CONTRACT = "supervisor_decision"
REVIEWER_DECISION_CONTRACT = "reviewer_decision"
WORKER_AGENT_OUTPUT_CONTRACT = "worker_agent_output"
CODING_INSPECTION_CONTRACT = "coding_inspection"
CODING_PATCH_PROPOSAL_CONTRACT = "coding_patch_proposal"
VERIFICATION_PLAN_CONTRACT = "verification_plan"
TOOL_RESULT_CONTRACT = "tool_result"


@dataclass(frozen=True)
class NodeContractSpec:
    id: str
    name: str
    node_kind: AgentGraphNodeKind
    payload_schema: type[BaseModel]
    description: str


class NodeContractRegistry:
    def __init__(self, contracts: list[NodeContractSpec] | None = None):
        self._contracts = {contract.id: contract for contract in (contracts or _default_contracts())}

    def get(self, contract_id: str) -> NodeContractSpec:
        try:
            return self._contracts[contract_id]
        except KeyError as exc:
            raise ValueError(f"unknown node contract: {contract_id}") from exc

    def list(self) -> list[NodeContractSpec]:
        return sorted(self._contracts.values(), key=lambda contract: contract.id)

    def validate_binding(
        self,
        contract_id: str,
        *,
        role_name: str,
        node_kind: AgentGraphNodeKind | str,
    ) -> NodeContractSpec:
        spec = self.get(contract_id)
        kind = AgentGraphNodeKind(node_kind)
        if spec.node_kind != kind:
            raise ValueError(f"contract {contract_id} is for {spec.node_kind.value} nodes, not {kind.value}")
        if role_name == RoleName.SUPERVISOR.value and contract_id != SUPERVISOR_DECISION_CONTRACT:
            raise ValueError("supervisor node must use supervisor_decision contract")
        if role_name == RoleName.REVIEWER.value and contract_id != REVIEWER_DECISION_CONTRACT:
            raise ValueError("reviewer node must use reviewer_decision contract")
        return spec

    def validate_payload(self, contract_id: str, payload: Any) -> BaseModel:
        return self.get(contract_id).payload_schema.model_validate(payload)


def default_contract_registry() -> NodeContractRegistry:
    return NodeContractRegistry()


def default_contract_for_role(role_name: str) -> str:
    if role_name == RoleName.SUPERVISOR.value:
        return SUPERVISOR_DECISION_CONTRACT
    if role_name == RoleName.REVIEWER.value:
        return REVIEWER_DECISION_CONTRACT
    return WORKER_AGENT_OUTPUT_CONTRACT


def _default_contracts() -> list[NodeContractSpec]:
    return [
        NodeContractSpec(
            id=SUPERVISOR_DECISION_CONTRACT,
            name="Supervisor decision",
            node_kind=AgentGraphNodeKind.CONTROL,
            payload_schema=SupervisorDecision,
            description="Structured routing plan produced by the supervisor node.",
        ),
        NodeContractSpec(
            id=REVIEWER_DECISION_CONTRACT,
            name="Reviewer decision",
            node_kind=AgentGraphNodeKind.CONTROL,
            payload_schema=ReviewerDecision,
            description="Structured review verdict produced by the reviewer node.",
        ),
        NodeContractSpec(
            id=WORKER_AGENT_OUTPUT_CONTRACT,
            name="Worker agent output",
            node_kind=AgentGraphNodeKind.WORKER,
            payload_schema=AgentOutput,
            description="Generic worker summary with tool results and risks.",
        ),
        NodeContractSpec(
            id=CODING_INSPECTION_CONTRACT,
            name="Coding inspection",
            node_kind=AgentGraphNodeKind.WORKER,
            payload_schema=CodingInspection,
            description="Repository inspection evidence used by the coding workflow.",
        ),
        NodeContractSpec(
            id=CODING_PATCH_PROPOSAL_CONTRACT,
            name="Coding patch proposal",
            node_kind=AgentGraphNodeKind.WORKER,
            payload_schema=PatchProposal,
            description="Patch proposal and verification commands for coding tasks.",
        ),
        NodeContractSpec(
            id=VERIFICATION_PLAN_CONTRACT,
            name="Verification plan",
            node_kind=AgentGraphNodeKind.WORKER,
            payload_schema=VerificationPlan,
            description="Focused verification command plan.",
        ),
        NodeContractSpec(
            id=TOOL_RESULT_CONTRACT,
            name="Tool result",
            node_kind=AgentGraphNodeKind.WORKER,
            payload_schema=ToolResult,
            description="Single tool execution result.",
        ),
    ]
