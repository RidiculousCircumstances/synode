from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
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
    planned_tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model_provider: str | None = None
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] = field(default_factory=dict)
    observability_trace_id: str | None = None
    tool_proxy_url: str | None = None
    tool_proxy_token: str | None = None
    tool_proxy_tools: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ApprovalRequest:
    tool_name: str
    action: str
    reason: str
    payload: dict[str, Any] = field(default_factory=dict)


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


class OpenHandsClient(Protocol):
    async def status(self) -> tuple[bool, str | None]: ...

    async def start_conversation(self, payload: dict[str, Any]) -> OpenHandsConversationState: ...

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
                response = await client.post("/api/conversations", json=_agent_server_payload(payload))
                response.raise_for_status()
                state = _conversation_state(response.json())
                run_response = await client.post(f"/api/conversations/{state.conversation_id}/run")
                run_response.raise_for_status()
                return state
            response = await client.post("/api/v1/app-conversations", json=_cloud_v1_payload(payload))
            response.raise_for_status()
            return _conversation_state(response.json())

    async def get_conversation(self, conversation_id: str) -> OpenHandsConversationState:
        async with self._client() as client:
            if self.api_mode == "agent_server":
                response = await client.get(f"/api/conversations/{conversation_id}")
            else:
                response = await client.get("/api/v1/app-conversations", params={"ids": conversation_id})
            response.raise_for_status()
            return _conversation_state(response.json(), conversation_id=conversation_id)

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
            response.raise_for_status()

    async def cancel_conversation(self, conversation_id: str) -> None:
        async with self._client() as client:
            if self.api_mode == "agent_server":
                response = await client.post(f"/api/conversations/{conversation_id}/pause")
            else:
                response = await client.post(f"/api/v1/app-conversations/{conversation_id}/pause")
                if response.status_code == 404:
                    response = await client.post(f"/api/v1/app-conversations/{conversation_id}/interrupt")
            response.raise_for_status()

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
        state = await client.start_conversation(_conversation_payload(node_input))
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
        while True:
            normalized = _normalize_status(current.status)
            if normalized == "finished":
                return await self._complete_output(node_input, current)
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
        payload = _contract_payload(node_input, state, summary)
        summary = str(payload.get("summary") or summary)
        content = {
            "runtime_backend": self.backend,
            "node_id": node_input.node_id,
            "contract_id": node_input.contract_id,
            "role": node_input.role,
            "status": "completed",
            "external_conversation_id": state.conversation_id,
            "summary": summary,
            "changed_files": _raw_list(state.raw, "changed_files"),
            "diff": _raw_str(state.raw, "diff"),
            "commands": _raw_list(state.raw, "commands"),
            "raw": state.raw,
        }
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


def _conversation_payload(node_input: NodeExecutionInput) -> dict[str, Any]:
    graph = node_input.agent_graph_snapshot or {}
    contract_schema = default_contract_registry().get(node_input.contract_id).payload_schema.model_json_schema()
    text = (
        f"Synode node: {node_input.node_id}\n"
        f"Synode role: {node_input.role}\n"
        f"Synode contract: {node_input.contract_id}\n"
        f"Mode: {node_input.mode}\n"
        f"Task: {node_input.task}\n"
        f"Node task: {node_input.plan_task or node_input.task}\n"
        f"Workspace: {node_input.workspace or '<none>'}\n\n"
        "Execute only this Synode graph node. Synode owns final review and approval state. "
        "Return only a JSON object matching the Synode contract schema. Do not wrap it in Markdown.\n\n"
        f"Role spec: {node_input.role_spec}\n"
        f"Planned tool calls: {node_input.planned_tool_calls}\n"
        f"Conversation context: {node_input.conversation_context}\n"
        f"Previous worker outputs: {node_input.previous_worker_outputs}\n"
        f"Upstream outputs: {node_input.upstream_outputs}\n"
        f"Graph snapshot: {graph}\n"
        f"Contract JSON schema: {contract_schema}\n"
    )
    payload: dict[str, Any] = {
        "initial_message": {"content": [{"type": "text", "text": text}]},
        "metadata": {
            "synode_run_id": node_input.run_id,
            "synode_thread_id": node_input.thread_id,
            "synode_node_id": node_input.node_id,
            "synode_role": node_input.role,
            "synode_contract_id": node_input.contract_id,
            "synode_trace_id": node_input.observability_trace_id,
        },
    }
    if node_input.tool_proxy_url and node_input.tool_proxy_token:
        payload["mcp_servers"] = {
            "synode": {
                "transport": "streamable_http",
                "url": node_input.tool_proxy_url,
                "headers": {"Authorization": f"Bearer {node_input.tool_proxy_token}"},
            }
        }
        payload["metadata"]["synode_mcp_proxy_url"] = node_input.tool_proxy_url
        payload["metadata"]["synode_mcp_tools"] = node_input.tool_proxy_tools
    if node_input.workspace:
        payload["workspace"] = node_input.workspace
    return payload


def _agent_server_payload(payload: dict[str, Any]) -> dict[str, Any]:
    transformed = dict(payload)
    initial_message = transformed.get("initial_message")
    if isinstance(initial_message, dict):
        transformed["initial_message"] = {
            **initial_message,
            "role": initial_message.get("role") or "user",
            "run": False,
        }
    workspace = transformed.pop("workspace", None)
    if isinstance(workspace, str) and workspace:
        transformed["workspace"] = {"working_dir": workspace}
    transformed.setdefault("confirmation_policy", {"kind": "AlwaysConfirm"})
    return transformed


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
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _raw_list(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key)
    return value if isinstance(value, list) else []


def _raw_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None
