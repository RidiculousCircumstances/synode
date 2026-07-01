from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from synode.config import Settings
from synode.persistence.database import Database
from synode.persistence.repository import Repository
from synode.schemas import ApprovalStatus, RuntimeBackend, ToolResult, ToolRisk


@dataclass(frozen=True)
class NodeExecutionInput:
    run_id: str
    thread_id: str
    role: str
    task: str
    workspace: str | None
    mode: str
    conversation_context: list[dict[str, Any]] = field(default_factory=list)
    previous_worker_outputs: list[dict[str, Any]] = field(default_factory=list)
    agent_graph_snapshot: dict[str, Any] = field(default_factory=dict)
    model_provider: str | None = None
    default_model_profile_id: str | None = None
    role_model_profile_ids: dict[str, str] = field(default_factory=dict)
    observability_trace_id: str | None = None


@dataclass(frozen=True)
class NodeExecutionOutput:
    role: str
    summary: str
    tool_results: list[ToolResult] = field(default_factory=list)
    risks: list[str] = field(default_factory=list)
    artifacts: list[dict[str, Any]] = field(default_factory=list)
    external_conversation_id: str | None = None


@dataclass(frozen=True)
class ExecutionBackendStatus:
    backend: RuntimeBackend
    available: bool
    detail: str | None = None


class NodeExecutionBackend(Protocol):
    backend: RuntimeBackend

    async def execute(self, node_input: NodeExecutionInput) -> NodeExecutionOutput: ...

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
    backend = RuntimeBackend.NATIVE_LANGGRAPH

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
    backend = RuntimeBackend.OPENHANDS

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
                return _approval_output(node_input.role, approval_id, conversation_id, artifact)
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
            "runtime_backend": self.backend.value,
            "external_conversation_id": state.conversation_id,
            "external_action_id": _action_id(pending_action),
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
                    "runtime_backend": self.backend.value,
                    "role": node_input.role,
                    "status": "waiting_approval",
                    "approval_id": approval.id,
                    "external_conversation_id": state.conversation_id,
                    "pending_action": pending_action,
                },
            )
        return _approval_output(node_input.role, approval.id, state.conversation_id, payload)

    async def _complete_output(
        self,
        node_input: NodeExecutionInput,
        state: OpenHandsConversationState,
    ) -> NodeExecutionOutput:
        summary = _summary_from_state(state)
        content = {
            "runtime_backend": self.backend.value,
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
            artifacts=[content],
            external_conversation_id=state.conversation_id,
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
        self._backends: dict[RuntimeBackend, NodeExecutionBackend] = {
            RuntimeBackend.NATIVE_LANGGRAPH: NativeLangGraphBackend(),
            RuntimeBackend.OPENHANDS: OpenHandsNodeBackend(settings, database, client=openhands_client),
        }

    def get(self, backend: RuntimeBackend | str) -> NodeExecutionBackend:
        return self._backends[RuntimeBackend(backend)]

    async def execute(self, backend: RuntimeBackend | str, node_input: NodeExecutionInput) -> NodeExecutionOutput:
        return await self.get(backend).execute(node_input)

    async def statuses(self) -> dict[RuntimeBackend, ExecutionBackendStatus]:
        statuses: dict[RuntimeBackend, ExecutionBackendStatus] = {}
        for backend, executor in self._backends.items():
            statuses[backend] = await executor.status()
        return statuses

    async def reject_approval(self, payload: Mapping[str, Any], reason: str) -> None:
        backend = payload.get("runtime_backend")
        if backend is None:
            return
        await self.get(RuntimeBackend(backend)).reject_approval(payload, reason)

    async def cancel_run(self, run_id: str) -> None:
        for executor in self._backends.values():
            await executor.cancel_run(run_id)


def build_execution_backend_registry(settings: Settings, database: Database) -> ExecutionBackendRegistry:
    return ExecutionBackendRegistry(settings, database)


def _conversation_payload(node_input: NodeExecutionInput) -> dict[str, Any]:
    graph = node_input.agent_graph_snapshot or {}
    text = (
        f"Synode role: {node_input.role}\n"
        f"Mode: {node_input.mode}\n"
        f"Task: {node_input.task}\n"
        f"Workspace: {node_input.workspace or '<none>'}\n\n"
        "Execute only this Synode graph node. Return a concise summary, changed files, "
        "commands run, and any risks. Synode owns final review and approval state.\n\n"
        f"Conversation context: {node_input.conversation_context}\n"
        f"Previous worker outputs: {node_input.previous_worker_outputs}\n"
        f"Graph snapshot: {graph}\n"
    )
    payload: dict[str, Any] = {
        "initial_message": {"content": [{"type": "text", "text": text}]},
        "metadata": {
            "synode_run_id": node_input.run_id,
            "synode_thread_id": node_input.thread_id,
            "synode_role": node_input.role,
            "synode_trace_id": node_input.observability_trace_id,
        },
    }
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
    role: str,
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
        role=role,
        summary=f"OpenHands is waiting for Synode approval: {approval_id}",
        tool_results=[result],
        risks=["approval required"],
        external_conversation_id=conversation_id,
    )


def _summary_from_state(state: OpenHandsConversationState) -> str:
    if state.final_message:
        return state.final_message
    for key in ("summary", "message", "result", "title"):
        value = state.raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return f"OpenHands conversation completed: {state.conversation_id}"


def _raw_list(raw: dict[str, Any], key: str) -> list[Any]:
    value = raw.get(key)
    return value if isinstance(value, list) else []


def _raw_str(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    return value if isinstance(value, str) else None
