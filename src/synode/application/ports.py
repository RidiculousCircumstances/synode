from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from synode.domain.models import NodeExecutionStatus, ToolCall, ToolResult

ModelStreamCallback = Callable[[str], Awaitable[None]]
RepositoryFactory = Callable[[Any], Any]
SandboxStatusFactory = Callable[[], Any]


class ModelRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    role: str
    prompt: str
    messages: list[dict[str, str]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    response_schema: type[BaseModel] | None = Field(default=None, exclude=True)
    temperature: float = 0.1
    timeout_seconds: float | None = None
    model_options: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    content: str
    structured: dict[str, Any] = Field(default_factory=dict)
    provider: str
    model: str | None = None
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    latency_ms: float | None = None


class ModelHealth(BaseModel):
    provider: str
    ok: bool
    model: str | None = None
    error: str | None = None


class ModelProvider(Protocol):
    name: str
    supports_streaming: bool

    async def invoke(self, request: ModelRequest) -> ModelResponse: ...

    async def health(self) -> ModelHealth: ...


class ModelRegistryPort(Protocol):
    def get(self, name: str) -> ModelProvider: ...

    def for_profile(self, profile: Any, api_key: str | None = None) -> ModelProvider: ...

    async def health(self) -> list[ModelHealth]: ...


class DatabasePort(Protocol):
    engine: Any

    def session(self) -> Any: ...

    async def close(self) -> None: ...


class ObservabilityPort(Protocol):
    def create_trace_id(self, seed: str | None = None) -> str | None: ...

    def observation(self, *args: Any, **kwargs: Any) -> Any: ...

    def update_current_span(self, *args: Any, **kwargs: Any) -> None: ...

    def update_current_generation(self, *args: Any, **kwargs: Any) -> None: ...

    def shutdown(self) -> None: ...


class SecretCipherPort(Protocol):
    def encrypt(self, value: str) -> str: ...

    def decrypt(self, value: str) -> str: ...


class ToolRegistryPort(Protocol):
    def get(self, name: str) -> Any: ...

    def list_names(self) -> list[str]: ...

    def unregister_prefix(self, prefix: str) -> None: ...


class ToolExecutorPort(Protocol):
    settings: Any
    tools: Any

    def allowed_tool_names(self, role_name: str) -> list[str]: ...

    def tool_catalog(self, role_name: str) -> list[dict[str, Any]]: ...

    async def create_proxy_session(
        self,
        *,
        run_id: str,
        thread_id: str,
        node_id: str,
        role_name: str,
        backend_id: str,
        workspace: str | None,
    ) -> Any: ...

    async def handle_mcp_proxy_request(
        self,
        *,
        session_id: str,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None: ...

    async def execute(
        self,
        run_id: str,
        role_name: str,
        workspace: str | None,
        call: ToolCall,
        approved_approval_id: str | None = None,
    ) -> ToolResult: ...


ToolExecutorFactory = Callable[[Any], ToolExecutorPort]


class RunQueuePort(Protocol):
    backend: str

    async def open(self) -> None: ...

    async def close(self) -> None: ...

    async def enqueue_run(self, run_id: str) -> None: ...

    async def reconcile_runs(self, run_ids: Iterable[str]) -> int: ...

    async def run_worker(
        self,
        *,
        worker_id: str,
        concurrency: int,
        wait: bool,
        handler: Any,
    ) -> bool: ...

    async def status(self) -> Any: ...


class MCPToolManagerPort(Protocol):
    async def discover(self, name: str, config: dict[str, Any]) -> list[str]: ...

    def register(self, tools: ToolRegistryPort, runtime_configs: list[dict[str, Any]]) -> None: ...


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


class ExecutionBackendPort(Protocol):
    def known_backend_ids(self) -> set[str]: ...

    def capabilities(self, backend: Any) -> Any: ...

    async def execute(self, backend: Any, node_input: NodeExecutionInput) -> NodeExecutionOutput: ...

    async def statuses(self) -> dict[str, ExecutionBackendStatus]: ...

    async def reject_approval(self, payload: Mapping[str, Any], reason: str) -> None: ...

    async def cancel_run(self, run_id: str) -> None: ...
