from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Protocol

import httpx

from synode.config import Settings
from synode.persistence.database import Database
from synode.persistence.repository import Repository
from synode.runtime.capabilities import (
    DEFAULT_BACKEND_CAPABILITIES,
    ExecutionBackendCapabilities,
    validate_backend_contract,
)
from synode.runtime.contracts import default_contract_registry
from synode.schemas import ApprovalStatus, NodeExecutionStatus, RuntimeBackend, ToolResult, ToolRisk


@dataclass(frozen=True)
class NodeExecutionInput:
    run_id: str
    thread_id: str
    node_id: str
    role: str
    backend_id: str
    contract_id: str
    task: str
    workspace: str | None
    mode: str
    conversation_context: list[dict[str, Any]] = field(default_factory=list)
    previous_worker_outputs: list[dict[str, Any]] = field(default_factory=list)
    upstream_outputs: list[dict[str, Any]] = field(default_factory=list)
    agent_graph_snapshot: dict[str, Any] = field(default_factory=dict)
    role_spec: dict[str, Any] = field(default_factory=dict)
    plan_task: str | None = None
    plan_steps: list[dict[str, Any]] = field(default_factory=list)
    planned_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model_provider: str | None = None
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] = field(default_factory=dict)
    observability_trace_id: str | None = None
    tool_proxy_url: str | None = None
    tool_proxy_token: str | None = None
    tool_proxy_tools: list[str] = field(default_factory=list)
    operator_response: dict[str, Any] | None = None


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    action: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class OperatorRequest:
    kind: str
    prompt: str
    context: dict[str, Any] = field(default_factory=dict)
    proposed_payload: dict[str, Any] = field(default_factory=dict)
    node_id: str | None = None
    role: str | None = None


@dataclass(frozen=True)
class NodeExecutionOutput:
    role: str
    summary: str
    status: NodeExecutionStatus = NodeExecutionStatus.COMPLETED
    node_id: str | None = None
    backend_id: str | None = None
    contract_id: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    tool_results: list[ToolResult] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    external_conversation_id: str | None = None
    approval_request: ApprovalRequest | None = None
    operator_request: OperatorRequest | None = None
    approval_id: str | None = None
    external_state: dict[str, Any] = field(default_factory=dict)


NodeExecutionResult = NodeExecutionOutput


@dataclass(frozen=True)
class ExecutionBackendStatus:
    backend: str
    available: bool
    detail: str | None = None


class NodeExecutionBackend(Protocol):
    backend: str

    async def execute(self, node_input: NodeExecutionInput) -> NodeExecutionResult: ...

    async def status(self) -> ExecutionBackendStatus: ...

    async def reject_approval(self, payload: Mapping[str, Any], reason: str) -> None: ...

    async def cancel_run(self, run_id: str) -> None: ...


@dataclass(frozen=True)
class OpenHandsConversationState:
    conversation_id: str
    status: str
    raw: dict[str, Any] = field(default_factory=dict)
    pending_actions: list[dict[str, Any]] = field(default_factory=list)
    final_message: str | None = None


class OpenHandsContractError(RuntimeError):
    def __init__(self, message: str, *, conversation_id: str, status: str, raw: dict[str, Any]):
        super().__init__(message)
        self.external_id = conversation_id
        self.external_state = {
            "conversation_id": conversation_id,
            "status": status,
            "final_message": raw.get("final_message"),
        }


class OpenHandsClient(Protocol):
    async def status(self) -> tuple[bool, str | None]: ...

    async def start_conversation(self, payload: dict[str, Any]) -> OpenHandsConversationState: ...

    async def send_message(self, conversation_id: str, text: str) -> OpenHandsConversationState: ...

    async def get_conversation(self, conversation_id: str) -> OpenHandsConversationState: ...

    async def respond_to_confirmation(
        self,
        conversation_id: str,
        *,
        accept: bool,
        reason: str | None = None,
    ) -> None: ...

    async def cancel_conversation(self, conversation_id: str) -> None: ...


class HttpOpenHandsClient:
    def __init__(self, settings: Settings):
        if not settings.openhands_base_url:
            raise RuntimeError("SYNODE_OPENHANDS_BASE_URL is required")
        self.base_url = settings.openhands_base_url.rstrip("/")
        self.api_key = settings.openhands_api_key
        self.api_mode = settings.openhands_api_mode
        self.timeout = settings.openhands_timeout_seconds

    async def status(self) -> tuple[bool, str | None]:
        try:
            async with self._client() as client:
                if self.api_mode == "agent_server":
                    response = await client.get("/health")
                    response.raise_for_status()
                    response = await client.get("/api/conversations/count")
                else:
                    response = await client.get("/api/v1/app-conversations/search", params={"limit": 1})
                response.raise_for_status()
            return True, f"OpenHands {self.api_mode} endpoint is reachable"
        except Exception as exc:
            return False, str(exc)

    async def start_conversation(self, payload: dict[str, Any]) -> OpenHandsConversationState:
        async with self._client() as client:
            if self.api_mode == "agent_server":
                request_payload = _agent_server_payload(payload)
                response = await client.post("/api/conversations", json=request_payload)
                _raise_openhands_for_status(response, "create conversation")
                state = _conversation_state(response.json())
                if not _agent_server_payload_auto_runs(request_payload) and _normalize_status(state.status) == "idle":
                    run_response = await client.post(f"/api/conversations/{state.conversation_id}/run")
                    if _is_openhands_already_running(run_response):
                        state_response = await client.get(f"/api/conversations/{state.conversation_id}")
                        _raise_openhands_for_status(state_response, "get conversation after start conflict")
                        state = _conversation_state(state_response.json(), conversation_id=state.conversation_id)
                        if _normalize_status(state.status) == "idle":
                            raise RuntimeError(
                                "OpenHands start conversation conflicted, but conversation is still idle"
                            )
                    else:
                        _raise_openhands_for_status(run_response, "start conversation")
                return state
            response = await client.post("/api/v1/app-conversations", json=_cloud_v1_payload(payload))
            _raise_openhands_for_status(response, "create conversation")
            return _conversation_state(response.json())

    async def send_message(self, conversation_id: str, text: str) -> OpenHandsConversationState:
        async with self._client() as client:
            path = (
                f"/api/conversations/{conversation_id}/events"
                if self.api_mode == "agent_server"
                else f"/api/v1/app-conversations/{conversation_id}/events"
            )
            response = await client.post(path, json=_message_payload(text))
            _raise_openhands_for_status(response, "send message")
        return await self.get_conversation(conversation_id)

    async def get_conversation(self, conversation_id: str) -> OpenHandsConversationState:
        async with self._client() as client:
            if self.api_mode == "agent_server":
                response = await client.get(f"/api/conversations/{conversation_id}")
            else:
                response = await client.get("/api/v1/app-conversations", params={"ids": conversation_id})
            _raise_openhands_for_status(response, "get conversation")
            state = _conversation_state(response.json(), conversation_id=conversation_id)
            if self.api_mode == "agent_server" and _normalize_status(state.status) == "finished":
                final_response = await client.get(f"/api/conversations/{conversation_id}/agent_final_response")
                _raise_openhands_for_status(final_response, "get final response")
                state = _with_final_response(state, final_response.json())
            return state

    async def respond_to_confirmation(
        self,
        conversation_id: str,
        *,
        accept: bool,
        reason: str | None = None,
    ) -> None:
        async with self._client() as client:
            path = (
                f"/api/conversations/{conversation_id}/events/respond_to_confirmation"
                if self.api_mode == "agent_server"
                else f"/api/v1/app-conversations/{conversation_id}/events/respond_to_confirmation"
            )
            response = await client.post(
                path,
                json={"accept": accept, "reason": reason},
            )
            _raise_openhands_for_status(response, "respond to confirmation")

    async def cancel_conversation(self, conversation_id: str) -> None:
        async with self._client() as client:
            if self.api_mode == "agent_server":
                response = await client.post(f"/api/conversations/{conversation_id}/pause")
            else:
                response = await client.post(f"/api/v1/app-conversations/{conversation_id}/pause")
                if response.status_code == 404:
                    response = await client.post(f"/api/v1/app-conversations/{conversation_id}/interrupt")
            _raise_openhands_for_status(response, "cancel conversation")

    def _client(self) -> httpx.AsyncClient:
        headers: dict[str, str] = {}
        if self.api_key and self.api_mode == "agent_server":
            headers["X-Session-API-Key"] = self.api_key
        elif self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return httpx.AsyncClient(base_url=self.base_url, headers=headers, timeout=self.timeout)


class NativeLangGraphBackend:
    backend = RuntimeBackend.NATIVE_LANGGRAPH.value

    async def execute(self, node_input: NodeExecutionInput) -> NodeExecutionOutput:
        raise RuntimeError("native_langgraph node execution is handled by the LangGraph runtime")

    async def status(self) -> ExecutionBackendStatus:
        return ExecutionBackendStatus(
            backend=self.backend,
            available=True,
            detail="native LangGraph backend is available",
        )

    async def reject_approval(self, payload: Mapping[str, Any], reason: str) -> None:
        return None

    async def cancel_run(self, run_id: str) -> None:
        return None


class OpenHandsNodeBackend:
    backend = RuntimeBackend.OPENHANDS.value

    def __init__(self, settings: Settings, database: Database, client: OpenHandsClient | None = None):
        self.settings = settings
        self.database = database
        self.client = client

    async def execute(self, node_input: NodeExecutionInput) -> NodeExecutionOutput:
        if not self.settings.openhands_enabled:
            raise RuntimeError("OpenHands backend is selected but SYNODE_OPENHANDS_ENABLED is false")
        client = self._client()
        existing = await self._load_node_artifact(node_input)
        if existing and existing.get("status") == "waiting_approval":
            return await self._resume_waiting_conversation(node_input, existing, client)
        state = await client.start_conversation(await _conversation_payload(node_input, self.settings, self.database))
        return await self._drive_conversation(node_input, client, state)

    async def status(self) -> ExecutionBackendStatus:
        if not self.settings.openhands_enabled:
            return ExecutionBackendStatus(
                backend=self.backend,
                available=False,
                detail="OpenHands backend is disabled",
            )
        try:
            available, detail = await self._client().status()
        except Exception as exc:
            return ExecutionBackendStatus(backend=self.backend, available=False, detail=str(exc))
        return ExecutionBackendStatus(backend=self.backend, available=available, detail=detail)

    async def reject_approval(self, payload: Mapping[str, Any], reason: str) -> None:
        conversation_id = payload.get("external_conversation_id")
        if not self.settings.openhands_enabled or not isinstance(conversation_id, str):
            return
        await self._client().respond_to_confirmation(conversation_id, accept=False, reason=reason)

    async def cancel_run(self, run_id: str) -> None:
        if not self.settings.openhands_enabled:
            return
        async with self.database.session() as session:
            repo = Repository(session)
            artifacts = await repo.list_artifacts(run_id, limit=1000)
            conversation_ids = {
                str(artifact.content.get("external_conversation_id"))
                for artifact in artifacts
                if artifact.kind.startswith("openhands_") and artifact.content.get("external_conversation_id")
            }
        for conversation_id in conversation_ids:
            await self._client().cancel_conversation(conversation_id)

    async def _resume_waiting_conversation(
        self,
        node_input: NodeExecutionInput,
        artifact: dict[str, Any],
        client: OpenHandsClient,
    ) -> NodeExecutionOutput:
        approval_id = artifact.get("approval_id")
        conversation_id = artifact.get("external_conversation_id")
        if not isinstance(approval_id, str) or not isinstance(conversation_id, str):
            raise RuntimeError("OpenHands waiting artifact is missing approval or conversation id")
        async with self.database.session() as session:
            repo = Repository(session)
            approval = await repo.get_approval(approval_id)
            if approval is None:
                raise RuntimeError(f"OpenHands approval not found: {approval_id}")
            if approval.status == ApprovalStatus.PENDING.value:
                return _approval_output(node_input, approval_id, conversation_id, artifact)
            if approval.status == ApprovalStatus.REJECTED.value:
                raise RuntimeError(f"OpenHands action was rejected: {approval.decision_reason or 'rejected'}")
        await client.respond_to_confirmation(conversation_id, accept=True, reason="Approved in Synode")
        state = await client.get_conversation(conversation_id)
        return await self._drive_conversation(node_input, client, state)

    async def _drive_conversation(
        self,
        node_input: NodeExecutionInput,
        client: OpenHandsClient,
        state: OpenHandsConversationState,
    ) -> NodeExecutionOutput:
        deadline = asyncio.get_running_loop().time() + self.settings.openhands_timeout_seconds
        current = state
        contract_repairs = 0
        while True:
            normalized = _normalize_status(current.status)
            if normalized == "finished":
                try:
                    return await self._complete_output(node_input, current)
                except OpenHandsContractError as exc:
                    if contract_repairs >= self.settings.openhands_contract_repair_attempts:
                        raise
                    contract_repairs += 1
                    current = await client.send_message(
                        current.conversation_id,
                        _contract_repair_message(node_input, current, str(exc), attempt=contract_repairs),
                    )
                    continue
            if normalized == "waiting_approval":
                return await self._create_approval_output(node_input, current)
            if normalized == "failed":
                raise RuntimeError(f"OpenHands conversation failed: {current.status}")
            if asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError(f"OpenHands conversation timed out: {current.conversation_id}")
            await asyncio.sleep(self.settings.openhands_poll_interval_seconds)
            current = await client.get_conversation(current.conversation_id)

    async def _create_approval_output(
        self,
        node_input: NodeExecutionInput,
        state: OpenHandsConversationState,
    ) -> NodeExecutionOutput:
        if not state.pending_actions:
            raise RuntimeError("OpenHands is waiting for confirmation but reported no pending actions")
        pending_action = state.pending_actions[0]
        payload = {
            "runtime_backend": self.backend,
            "external_conversation_id": state.conversation_id,
            "external_action_id": _action_id(pending_action),
            "synode_node_id": node_input.node_id,
            "synode_contract_id": node_input.contract_id,
            "synode_role": node_input.role,
            "openhands_status": state.status,
            "openhands_action": pending_action,
        }
        async with self.database.session() as session:
            repo = Repository(session)
            approval = await repo.create_approval(
                node_input.run_id,
                _tool_name(pending_action),
                str(pending_action.get("command") or pending_action.get("action") or "confirm"),
                "OpenHands requested confirmation before executing an action.",
                payload,
            )
            await repo.add_artifact(
                node_input.run_id,
                _artifact_kind(node_input.role),
                {
                    "runtime_backend": self.backend,
                    "node_id": node_input.node_id,
                    "contract_id": node_input.contract_id,
                    "role": node_input.role,
                    "status": "waiting_approval",
                    "approval_id": approval.id,
                    "external_conversation_id": state.conversation_id,
                    "pending_action": pending_action,
                },
            )
        return _approval_output(node_input, approval.id, state.conversation_id, payload)

    async def _complete_output(
        self,
        node_input: NodeExecutionInput,
        state: OpenHandsConversationState,
    ) -> NodeExecutionOutput:
        summary = _summary_from_state(state)
        base_content = {
            "runtime_backend": self.backend,
            "node_id": node_input.node_id,
            "contract_id": node_input.contract_id,
            "role": node_input.role,
            "external_conversation_id": state.conversation_id,
            "summary": summary,
            "changed_files": _raw_list(state.raw, "changed_files"),
            "diff": _raw_str(state.raw, "diff"),
            "commands": _raw_list(state.raw, "commands"),
            "raw": state.raw,
        }
        try:
            payload = _contract_payload(node_input, state, summary)
            await self._validate_grounded_output(node_input, state, payload)
        except Exception as exc:
            content = {
                **base_content,
                "status": "contract_error",
                "error": str(exc),
            }
            async with self.database.session() as session:
                repo = Repository(session)
                await repo.add_artifact(node_input.run_id, _artifact_kind(node_input.role), content)
            raise OpenHandsContractError(
                str(exc),
                conversation_id=state.conversation_id,
                status=state.status,
                raw=state.raw,
            ) from exc
        summary = str(payload.get("summary") or summary)
        content = {**base_content, "status": "completed", "summary": summary}
        async with self.database.session() as session:
            repo = Repository(session)
            await repo.add_artifact(node_input.run_id, _artifact_kind(node_input.role), content)
        return NodeExecutionOutput(
            role=node_input.role,
            summary=summary,
            status=NodeExecutionStatus.COMPLETED,
            node_id=node_input.node_id,
            backend_id=node_input.backend_id,
            contract_id=node_input.contract_id,
            payload=payload,
            artifacts=[content],
            external_conversation_id=state.conversation_id,
            external_state={"conversation_id": state.conversation_id, "status": state.status},
        )

    async def _validate_grounded_output(
        self,
        node_input: NodeExecutionInput,
        state: OpenHandsConversationState,
        payload: dict[str, Any],
    ) -> None:
        if node_input.model_provider == "fake" or not node_input.tool_proxy_url:
            return
        if node_input.role != "coder" and not payload.get("tool_results"):
            return
        async with self.database.session() as session:
            repo = Repository(session)
            audits = await repo.list_tool_audit(node_input.run_id, limit=500)
        role_audits = [audit for audit in audits if audit.role == node_input.role]
        if role_audits:
            return
        reported_tools = [
            str(item.get("tool_name"))
            for item in payload.get("tool_results", [])
            if isinstance(item, dict) and item.get("tool_name")
        ]
        raise RuntimeError(
            "OpenHands final output is not grounded in Synode MCP tool audit evidence; "
            f"conversation_id={state.conversation_id}; reported_tools={reported_tools}"
        )

    async def _load_node_artifact(self, node_input: NodeExecutionInput) -> dict[str, Any] | None:
        async with self.database.session() as session:
            repo = Repository(session)
            artifact = await repo.get_latest_artifact(node_input.run_id, _artifact_kind(node_input.role))
            return artifact.content if artifact is not None else None

    def _client(self) -> OpenHandsClient:
        if self.client is not None:
            return self.client
        return HttpOpenHandsClient(self.settings)


class ExecutionBackendRegistry:
    def __init__(self, settings: Settings, database: Database, openhands_client: OpenHandsClient | None = None):
        self._database = database
        self._backends: dict[str, NodeExecutionBackend] = {
            RuntimeBackend.NATIVE_LANGGRAPH.value: NativeLangGraphBackend(),
            RuntimeBackend.OPENHANDS.value: OpenHandsNodeBackend(settings, database, client=openhands_client),
        }

    def get(self, backend: RuntimeBackend | str) -> NodeExecutionBackend:
        backend_id = _backend_id(backend)
        try:
            return self._backends[backend_id]
        except KeyError as exc:
            raise ValueError(f"unknown execution backend: {backend_id}") from exc

    def known_backend_ids(self) -> set[str]:
        return set(self._backends)

    def capabilities(self, backend: RuntimeBackend | str) -> ExecutionBackendCapabilities:
        backend_id = _backend_id(backend)
        if backend_id not in self._backends:
            raise ValueError(f"unknown execution backend: {backend_id}")
        return DEFAULT_BACKEND_CAPABILITIES[backend_id]

    def validate_contract(self, backend: RuntimeBackend | str, contract_id: str) -> None:
        backend_id = _backend_id(backend)
        if backend_id not in self._backends:
            raise ValueError(f"unknown execution backend: {backend_id}")
        validate_backend_contract(backend_id, contract_id)

    async def execute(self, backend: RuntimeBackend | str, node_input: NodeExecutionInput) -> NodeExecutionResult:
        backend_id = _backend_id(backend)
        try:
            output = await self.get(backend_id).execute(node_input)
        except asyncio.CancelledError:
            await self._persist_node_state(node_input, NodeExecutionStatus.CANCELLED)
            raise
        except Exception as exc:
            await self._persist_node_state(
                node_input,
                NodeExecutionStatus.FAILED,
                external_id=getattr(exc, "external_id", None),
                external_state=getattr(exc, "external_state", None),
                last_error=str(exc),
            )
            raise
        await self._persist_node_state(
            node_input,
            output.status,
            approval_id=output.approval_id,
            external_id=output.external_conversation_id,
            external_state=output.external_state,
        )
        return output

    async def statuses(self) -> dict[str, ExecutionBackendStatus]:
        statuses: dict[str, ExecutionBackendStatus] = {}
        for backend, executor in self._backends.items():
            statuses[backend] = await executor.status()
        return statuses

    async def reject_approval(self, payload: Mapping[str, Any], reason: str) -> None:
        backend = payload.get("runtime_backend")
        if backend is None:
            return
        await self.get(str(backend)).reject_approval(payload, reason)

    async def cancel_run(self, run_id: str) -> None:
        for executor in self._backends.values():
            await executor.cancel_run(run_id)

    async def _persist_node_state(
        self,
        node_input: NodeExecutionInput,
        status: NodeExecutionStatus,
        *,
        approval_id: str | None = None,
        external_id: str | None = None,
        external_state: dict[str, Any] | None = None,
        last_error: str | None = None,
    ) -> None:
        async with self._database.session() as session:
            repo = Repository(session)
            await repo.upsert_runtime_node_state(
                node_input.run_id,
                node_input.node_id,
                node_input.role,
                node_input.backend_id,
                node_input.contract_id,
                status,
                external_id=external_id,
                approval_id=approval_id,
                external_state=external_state,
                last_error=last_error,
            )


def build_execution_backend_registry(settings: Settings, database: Database) -> ExecutionBackendRegistry:
    return ExecutionBackendRegistry(settings, database)


def _backend_id(backend: RuntimeBackend | str) -> str:
    return backend.value if isinstance(backend, RuntimeBackend) else str(backend)


async def _conversation_payload(
    node_input: NodeExecutionInput,
    settings: Settings,
    database: Database,
) -> dict[str, Any]:
    graph = node_input.agent_graph_snapshot or {}
    contract_schema = default_contract_registry().get(node_input.contract_id).payload_schema.model_json_schema()
    workspace = _openhands_workspace_path(settings, node_input.workspace)
    role_plan_steps = [
        step
        for step in node_input.plan_steps
        if isinstance(step, dict) and str(step.get("role") or "") == node_input.role
    ]
    if not role_plan_steps and node_input.plan_task:
        role_plan_steps = [{"role": node_input.role, "task": node_input.plan_task, "tool_calls": node_input.planned_tool_calls}]
    text = (
        f"Synode node: {node_input.node_id}\n"
        f"Synode role: {node_input.role}\n"
        f"Synode contract: {node_input.contract_id}\n"
        f"Mode: {node_input.mode}\n"
        f"Full user task: {node_input.task}\n"
        f"Current node hint: {node_input.plan_task or '<none>'}\n"
        f"Workspace: {workspace or node_input.workspace or '<none>'}\n\n"
        "Execute only this Synode graph node. Synode owns final review and approval state. "
        "Use available OpenHands and Synode MCP tools to inspect, edit, and verify the workspace as needed. "
        "Do not finish after inspection when the full user task requires code changes or verification. "
        "Do not write simulated tool output in your message; call the actual tools and wait for observations. "
        "A final object shaped like a single tool result is invalid for this node. "
        "When this node's work is complete, finish with exactly one JSON object matching the Synode contract schema. "
        "Do not wrap the final JSON object in Markdown. The final object must include role and summary.\n\n"
        "Required coding workflow when code changes are requested: list/read/search files, apply the minimal patch, "
        "run pytest -q, then finish with the Synode contract JSON.\n\n"
        f"Role spec: {node_input.role_spec}\n"
        f"Role plan steps: {role_plan_steps}\n"
        f"Planned tool calls: {node_input.planned_tool_calls}\n"
        f"Conversation context: {node_input.conversation_context}\n"
        f"Previous worker outputs: {node_input.previous_worker_outputs}\n"
        f"Upstream outputs: {node_input.upstream_outputs}\n"
        f"Operator response: {node_input.operator_response or {}}\n"
        f"Graph snapshot: {graph}\n"
        f"Contract JSON schema: {contract_schema}\n"
    )
    metadata: dict[str, Any] = {
        "synode_run_id": node_input.run_id,
        "synode_thread_id": node_input.thread_id,
        "synode_node_id": node_input.node_id,
        "synode_role": node_input.role,
        "synode_contract_id": node_input.contract_id,
        "synode_trace_id": node_input.observability_trace_id,
    }
    agent_settings = await _openhands_agent_settings(node_input, settings, database)
    payload: dict[str, Any] = {
        "initial_message": {"content": [{"type": "text", "text": text}]},
        "metadata": metadata,
        "agent_settings": agent_settings,
        "max_iterations": settings.openhands_max_iterations,
        "autotitle": False,
    }
    if node_input.tool_proxy_url and node_input.tool_proxy_token:
        agent_settings["mcp_config"] = {
            "mcpServers": {
                "synode": {
                    "type": "streamable-http",
                    "url": node_input.tool_proxy_url,
                    "headers": {"Authorization": f"Bearer {node_input.tool_proxy_token}"},
                }
            }
        }
        metadata["synode_mcp_proxy_url"] = node_input.tool_proxy_url
        metadata["synode_mcp_tools"] = node_input.tool_proxy_tools
    if workspace:
        payload["workspace"] = workspace
    return payload


async def _openhands_agent_settings(
    node_input: NodeExecutionInput,
    settings: Settings,
    database: Database,
) -> dict[str, Any]:
    return {
        "schema_version": 4,
        "agent_kind": "openhands",
        "agent": "CodeActAgent",
        "llm": await _openhands_llm_settings(node_input, settings, database),
        "tools": [],
        "mcp_config": {},
        "enable_sub_agents": False,
        "enable_switch_llm_tool": True,
        "tool_concurrency_limit": 1,
    }


async def _openhands_llm_settings(
    node_input: NodeExecutionInput,
    settings: Settings,
    database: Database,
) -> dict[str, Any]:
    profile_id = node_input.role_model_profile_ids.get(node_input.role) or node_input.default_model_profile_id
    provider_type = node_input.model_provider or settings.model_provider
    model = settings.ollama_model
    base_url: str | None = settings.ollama_base_url
    options: dict[str, Any] = {}
    if profile_id:
        async with database.session() as session:
            repo = Repository(session)
            profile = await repo.get_model_profile(profile_id)
            if profile is None:
                raise LookupError(f"model profile not found: {profile_id}")
            if not profile.enabled:
                raise RuntimeError(f"model profile is disabled: {profile.name}")
            provider_type = profile.provider_type
            model = profile.model
            base_url = profile.base_url or base_url
            options = dict(profile.options or {})

    llm_model = _openhands_model_name(str(provider_type), model)
    llm: dict[str, Any] = {
        "model": llm_model,
        "api_key": str(options.get("api_key") or "ollama") if str(provider_type) == "ollama" else options.get("api_key"),
        "auth_type": "api_key",
        "timeout": int(options.get("timeout") or options.get("timeout_seconds") or settings.model_timeout_seconds),
    }
    if base_url:
        llm["base_url"] = base_url
    if "temperature" in options:
        llm["temperature"] = options["temperature"]
    if "top_p" in options:
        llm["top_p"] = options["top_p"]
    if "top_k" in options:
        llm["top_k"] = options["top_k"]
    max_output_tokens = options.get("max_output_tokens") or options.get("num_predict")
    if max_output_tokens is not None:
        llm["max_output_tokens"] = int(max_output_tokens)
    if "native_tool_calling" in options:
        llm["native_tool_calling"] = bool(options["native_tool_calling"])
    if str(provider_type) == "ollama":
        llm["reasoning_effort"] = "none"
        llm["enable_encrypted_reasoning"] = False
    return {key: value for key, value in llm.items() if value is not None}


def _openhands_model_name(provider_type: str, model: str) -> str:
    if provider_type == "ollama" and not model.startswith("ollama/"):
        return f"ollama/{model}"
    return model


def _openhands_workspace_path(settings: Settings, workspace: str | None) -> str | None:
    if not workspace:
        return None
    container_root = settings.openhands_container_workspace or settings.sandbox_docker_container_workspace
    host_root = settings.openhands_host_workspace or settings.sandbox_docker_host_workspace
    if not container_root or not host_root:
        return workspace
    try:
        workspace_path = PurePosixPath(workspace)
        container_path = PurePosixPath(container_root)
        relative = workspace_path.relative_to(container_path)
    except ValueError:
        return workspace
    return str(PurePosixPath(host_root).joinpath(relative))


def _agent_server_payload(payload: dict[str, Any]) -> dict[str, Any]:
    transformed = dict(payload)
    metadata = transformed.pop("metadata", None)
    if isinstance(metadata, dict):
        transformed.setdefault("observability_metadata", _observability_metadata(metadata))
    initial_message = transformed.get("initial_message")
    if isinstance(initial_message, dict):
        transformed["initial_message"] = {
            **initial_message,
            "role": initial_message.get("role") or "user",
            "run": True,
        }
    workspace = transformed.pop("workspace", None)
    if isinstance(workspace, str) and workspace:
        transformed["workspace"] = {"working_dir": workspace, "kind": "LocalWorkspace"}
    elif isinstance(workspace, dict):
        transformed["workspace"] = {**workspace, "kind": workspace.get("kind") or "LocalWorkspace"}
    transformed.setdefault("confirmation_policy", {"kind": "AlwaysConfirm"})
    return transformed


def _agent_server_payload_auto_runs(payload: Mapping[str, Any]) -> bool:
    initial_message = payload.get("initial_message")
    return isinstance(initial_message, dict) and initial_message.get("run") is True


def _message_payload(text: str) -> dict[str, Any]:
    return {"role": "user", "run": True, "content": [{"type": "text", "text": text}]}


def _observability_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    clean: dict[str, Any] = {}
    for key, value in metadata.items():
        if value is None:
            continue
        if isinstance(value, str | int | float | bool):
            clean[str(key)] = value
        elif isinstance(value, list) and all(isinstance(item, str | int | float | bool) for item in value):
            clean[str(key)] = value
        else:
            clean[str(key)] = json.dumps(value, sort_keys=True)
    return clean


def _raise_openhands_for_status(response: httpx.Response, action: str) -> None:
    try:
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        body = response.text.strip()
        if len(body) > 2000:
            body = f"{body[:2000]}..."
        raise RuntimeError(
            f"OpenHands {action} failed with HTTP {response.status_code}: {body or response.reason_phrase}"
        ) from exc


def _is_openhands_already_running(response: httpx.Response) -> bool:
    return response.status_code == 409 and "already running" in response.text.lower()


def _with_final_response(state: OpenHandsConversationState, data: Any) -> OpenHandsConversationState:
    response = data.get("response") if isinstance(data, dict) else None
    if not isinstance(response, str) or not response.strip():
        return state
    raw = dict(state.raw)
    raw["final_message"] = response
    return OpenHandsConversationState(
        conversation_id=state.conversation_id,
        status=state.status,
        raw=raw,
        pending_actions=state.pending_actions,
        final_message=response,
    )


def _cloud_v1_payload(payload: dict[str, Any]) -> dict[str, Any]:
    transformed = dict(payload)
    transformed.pop("workspace", None)
    return transformed


def _conversation_state(data: Any, conversation_id: str | None = None) -> OpenHandsConversationState:
    raw = _response_payload(data)
    if not isinstance(raw, dict):
        raise RuntimeError("OpenHands response envelope is not an object")
    cid = conversation_id or raw.get("conversation_id") or raw.get("app_conversation_id") or raw.get("id")
    if not isinstance(cid, str) or not cid:
        raise RuntimeError("OpenHands response did not include a conversation id")
    status = raw.get("execution_status") or raw.get("agent_status") or raw.get("status") or "running"
    pending = raw.get("pending_actions") or raw.get("unmatched_actions") or raw.get("actions") or []
    if not isinstance(pending, list):
        pending = []
    final_message = raw.get("final_message") or raw.get("summary") or raw.get("result")
    return OpenHandsConversationState(
        conversation_id=cid,
        status=str(status),
        raw=raw,
        pending_actions=[item for item in pending if isinstance(item, dict)],
        final_message=str(final_message) if final_message is not None else None,
    )


def _response_payload(data: Any) -> dict[str, Any]:
    if isinstance(data, list) and data and isinstance(data[0], dict):
        return data[0]
    if isinstance(data, dict):
        for key in ("conversation", "item"):
            value = data.get(key)
            if isinstance(value, dict):
                return value
        for key in ("items", "results", "conversations"):
            value = data.get(key)
            if isinstance(value, list) and value and isinstance(value[0], dict):
                return value[0]
        return data
    raise RuntimeError("OpenHands response envelope is not an object")


def _normalize_status(status: str) -> str:
    value = status.lower()
    if value in {"idle", "created", "ready"}:
        return "idle"
    if value in {"finished", "completed", "complete", "done", "success", "succeeded"}:
        return "finished"
    if value in {"waiting_for_confirmation", "awaiting_confirmation", "requires_confirmation", "confirmation"}:
        return "waiting_approval"
    if value in {"failed", "failure", "error", "stuck", "cancelled", "canceled"}:
        return "failed"
    return "running"


def _artifact_kind(role: str) -> str:
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in role)
    return f"openhands_{safe[:60]}"


def _tool_name(action: dict[str, Any]) -> str:
    value = action.get("tool_name") or action.get("tool") or action.get("name") or action.get("action") or "action"
    return f"openhands.{value}"


def _action_id(action: dict[str, Any]) -> str | None:
    value = action.get("id") or action.get("action_id") or action.get("event_id")
    return str(value) if value is not None else None


def _approval_output(
    node_input: NodeExecutionInput,
    approval_id: str,
    conversation_id: str,
    payload: Mapping[str, Any],
) -> NodeExecutionOutput:
    result = ToolResult(
        tool_name="openhands.confirmation",
        ok=False,
        risk=ToolRisk.WRITE,
        output={"runtime_backend": RuntimeBackend.OPENHANDS.value, "payload": dict(payload)},
        error="approval required",
        approval_id=approval_id,
    )
    return NodeExecutionOutput(
        role=node_input.role,
        summary=f"OpenHands is waiting for Synode approval: {approval_id}",
        status=NodeExecutionStatus.WAITING_APPROVAL,
        node_id=node_input.node_id,
        backend_id=node_input.backend_id,
        contract_id=node_input.contract_id,
        payload={"approval_id": approval_id, "external_conversation_id": conversation_id},
        tool_results=[result],
        risks=["approval required"],
        external_conversation_id=conversation_id,
        approval_id=approval_id,
        external_state={"conversation_id": conversation_id, "status": "waiting_approval"},
    )


def _summary_from_state(state: OpenHandsConversationState) -> str:
    if state.final_message:
        return state.final_message
    for key in ("summary", "message", "result", "title"):
        value = state.raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"OpenHands conversation completed: {state.conversation_id}"


def _contract_payload(
    node_input: NodeExecutionInput,
    state: OpenHandsConversationState,
    summary: str,
) -> dict[str, Any]:
    payload = _structured_payload_from_state(state)
    if payload is None:
        raise RuntimeError(
            f"OpenHands did not return JSON payload for Synode contract {node_input.contract_id}"
        )
    validated = default_contract_registry().validate_payload(node_input.contract_id, payload)
    return validated.model_dump(mode="json")


def _structured_payload_from_state(state: OpenHandsConversationState) -> dict[str, Any] | None:
    for key in ("synode_payload", "structured_payload", "payload", "result"):
        value = state.raw.get(key)
        if isinstance(value, dict):
            return value
        if isinstance(value, str):
            parsed = _parse_json_object(value)
            if parsed is not None:
                return parsed
    if state.final_message:
        parsed = _parse_json_object(state.final_message)
        if parsed is not None:
            return parsed
    return None


def _parse_json_object(value: str) -> dict[str, Any] | None:
    decoder = json.JSONDecoder()
    text = value.strip()
    candidates = [text]
    first_object = text.find("{")
    if first_object > 0:
        candidates.append(text[first_object:])
    for candidate in candidates:
        try:
            parsed, _ = decoder.raw_decode(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _contract_repair_message(
    node_input: NodeExecutionInput,
    state: OpenHandsConversationState,
    error: str,
    *,
    attempt: int,
) -> str:
    final_message = _truncate_text(state.final_message or "", 1600)
    return (
        f"Synode rejected your previous final response for node {node_input.node_id} "
        f"(repair attempt {attempt}).\n"
        f"Validation error: {error}\n\n"
        f"Previous final response:\n{final_message or '<empty>'}\n\n"
        "Continue the same task in this conversation. Do not start over and do not create a new conversation. "
        "Do not fabricate tool results. Use the actual available tools for repository inspection, edits, and verification. "
        "If no real tool observation has happened yet, your next step should be an actual tool call such as native.fs_list, "
        "native.fs_read/native.fs_search, native.patch_apply or native.fs_write, and native.shell with pytest -q. "
        "Only after the required work is complete, finish with exactly this Synode node contract shape:\n"
        '{"role":"'
        f'{node_input.role}","summary":"what changed and how it was verified","tool_results":[],"risks":[]'
        "}\n"
        "Do not wrap the final JSON in Markdown."
    )


def _truncate_text(value: str, max_chars: int) -> str:
    if len(value) <= max_chars:
        return value
    return f"{value[:max_chars].rstrip()} ...[truncated]"


def _raw_list(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key)
    return value if isinstance(value, list) else []


def _raw_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None
