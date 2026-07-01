from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from synode.models.provider import ModelProviderRegistry
from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.runtime.execution import (
    ExecutionBackendRegistry,
    HttpOpenHandsClient,
    OpenHandsConversationState,
)
from synode.runtime.queue import InMemoryRunQueueTransport
from synode.runtime.service import OrchestrationService
from synode.schemas import (
    AgentGraphCreateRequest,
    ApprovalStatus,
    ModelProfileCreateRequest,
    ModelProviderType,
    RunMode,
    RunStatus,
    RuntimeBackend,
    SecretCreateRequest,
)
from synode.tools import build_tool_registry


def _graph_payload(
    roles: dict[str, object],
    *,
    name: str,
    workers: list[str],
    node_runtime_bindings: dict[str, RuntimeBackend] | None = None,
    node_contracts: dict[str, str] | None = None,
    default_model_profile_id: str | None = None,
) -> AgentGraphCreateRequest:
    role_names = ["supervisor", *workers, "reviewer"]
    nodes = [
        {
            "id": role_name,
            "role_id": getattr(roles[role_name], "id"),
            "label": role_name,
            "kind": "control" if role_name in {"supervisor", "reviewer"} else "worker",
        }
        for role_name in role_names
    ]
    return AgentGraphCreateRequest(
        name=name,
        nodes=nodes,
        node_edges=[
            {"from_node": role_names[index], "to_node": role_names[index + 1]}
            for index in range(len(role_names) - 1)
        ],
        node_runtime_bindings=node_runtime_bindings or {},
        node_contracts=node_contracts or {},
        default_model_profile_id=default_model_profile_id,
    )


async def test_custom_graph_and_model_profile_drive_run(service, tmp_path: pathlib.Path) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n2026-06-02,20\n", encoding="utf-8")
    profile = await service.create_model_profile(
        ModelProfileCreateRequest(
            name="fake test profile",
            provider_type=ModelProviderType.FAKE,
            model="fake",
        )
    )
    roles = {role.name: role for role in await service.list_agent_roles()}
    graph = await service.create_agent_graph(
        _graph_payload(
            roles,
            name="analysis-only test graph",
            workers=["data_analyst"],
            default_model_profile_id=profile.id,
        )
    )

    result = await service.run_task(
        "Analyze sample data and summarize findings",
        workspace=str(tmp_path),
        default_model_profile_id=profile.id,
        agent_graph_id=graph.id,
    )

    assert result.status == RunStatus.COMPLETED
    assert result.model_provider == ModelProviderType.FAKE.value
    assert result.default_model_profile_id == profile.id
    assert result.agent_graph_id == graph.id
    assert result.agent_graph_snapshot["name"] == graph.name
    assert result.agent_graph_snapshot["node_runtime_bindings"]["data_analyst"] == RuntimeBackend.NATIVE_LANGGRAPH
    assert "data_analyst" in (result.final_answer or "")


async def test_graph_runtime_bindings_reject_disabled_openhands(service, tmp_path: pathlib.Path) -> None:
    roles = {role.name: role for role in await service.list_agent_roles()}
    graph = await service.create_agent_graph(
        _graph_payload(
            roles,
            name="openhands disabled graph",
            workers=["coder"],
            node_runtime_bindings={"coder": RuntimeBackend.OPENHANDS},
        )
    )

    with pytest.raises(ValueError, match="OpenHands"):
        await service.create_run(
            "Use OpenHands",
            workspace=str(tmp_path),
            model_provider="fake",
            mode=RunMode.CODING,
            agent_graph_id=graph.id,
        )


async def test_graph_runtime_bindings_reject_system_roles(service) -> None:
    roles = {role.name: role for role in await service.list_agent_roles()}

    with pytest.raises(ValueError, match="supervisor and reviewer"):
        await service.create_agent_graph(
            _graph_payload(
                roles,
                name="invalid system runtime graph",
                workers=["coder"],
                node_runtime_bindings={"reviewer": RuntimeBackend.OPENHANDS},
            )
        )


async def test_openhands_node_backend_completes_coding_run(settings, database, tmp_path: pathlib.Path) -> None:
    service = await _openhands_service(settings, database, _FakeOpenHandsClient(["completed"]))
    roles = {role.name: role for role in await service.list_agent_roles()}
    graph = await service.create_agent_graph(
        _graph_payload(
            roles,
            name="openhands coder graph",
            workers=["coder"],
            node_runtime_bindings={"coder": RuntimeBackend.OPENHANDS},
        )
    )

    result = await service.run_task(
        "Let OpenHands inspect the repository",
        workspace=str(tmp_path),
        model_provider="fake",
        mode=RunMode.CODING,
        agent_graph_id=graph.id,
    )
    artifacts = await service.list_artifacts(result.id)

    assert result.status == RunStatus.COMPLETED
    assert result.agent_graph_snapshot["node_runtime_bindings"]["coder"] == RuntimeBackend.OPENHANDS
    assert "OpenHands completed" in (result.final_answer or "")
    assert any(artifact.kind == "openhands_coder" for artifact in artifacts)


async def test_agent_graph_v2_node_backend_contracts_drive_openhands(
    settings,
    database,
    tmp_path: pathlib.Path,
) -> None:
    service = await _openhands_service(settings, database, _FakeOpenHandsClient(["completed"]))
    roles = {role.name: role for role in await service.list_agent_roles()}
    graph = await service.create_agent_graph(
        _graph_payload(
            roles,
            name="openhands node binding graph",
            workers=["coder"],
            node_runtime_bindings={"coder": RuntimeBackend.OPENHANDS},
            node_contracts={"coder": "worker_agent_output"},
        )
    )

    result = await service.run_task(
        "Let the coder node use its bound backend",
        workspace=str(tmp_path),
        model_provider="fake",
        mode=RunMode.CODING,
        agent_graph_id=graph.id,
    )
    async with database.session() as session:
        states = await Repository(session).list_runtime_node_states(result.id)

    assert result.status == RunStatus.COMPLETED
    assert result.agent_graph_snapshot["node_runtime_bindings"]["coder"] == RuntimeBackend.OPENHANDS
    assert result.agent_graph_snapshot["node_contracts"]["coder"] == "worker_agent_output"
    assert any(
        state.node_id == "coder"
        and state.backend_id == RuntimeBackend.OPENHANDS.value
        and state.contract_id == "worker_agent_output"
        and state.status == "completed"
        for state in states
    )


async def test_agent_graph_v2_rejects_duplicate_role_nodes(service) -> None:
    roles = {role.name: role for role in await service.list_agent_roles()}

    with pytest.raises(ValueError, match="duplicate agent graph role node"):
        await service.create_agent_graph(
            AgentGraphCreateRequest(
                name="duplicate role node graph",
                nodes=[
                    {"id": "supervisor", "role_id": roles["supervisor"].id, "label": "supervisor", "kind": "control"},
                    {"id": "coder_a", "role_id": roles["coder"].id, "label": "coder", "kind": "worker"},
                    {"id": "coder_b", "role_id": roles["coder"].id, "label": "coder", "kind": "worker"},
                    {"id": "reviewer", "role_id": roles["reviewer"].id, "label": "reviewer", "kind": "control"},
                ],
                node_edges=[
                    {"from_node": "supervisor", "to_node": "coder_a"},
                    {"from_node": "coder_a", "to_node": "reviewer"},
                ],
            )
        )


async def test_openhands_confirmation_uses_synode_approval(settings, database, tmp_path: pathlib.Path) -> None:
    client = _FakeOpenHandsClient(["waiting_for_confirmation", "completed"])
    service = await _openhands_service(settings, database, client)
    roles = {role.name: role for role in await service.list_agent_roles()}
    graph = await service.create_agent_graph(
        _graph_payload(
            roles,
            name="openhands approval graph",
            workers=["coder"],
            node_runtime_bindings={"coder": RuntimeBackend.OPENHANDS},
        )
    )
    first = await service.run_task(
        "Let OpenHands edit the repository",
        workspace=str(tmp_path),
        model_provider="fake",
        mode=RunMode.CODING,
        agent_graph_id=graph.id,
    )
    approvals = await service.list_approvals(run_id=first.id)

    assert first.status == RunStatus.WAITING_APPROVAL
    assert len(approvals) == 1
    assert approvals[0].payload["runtime_backend"] == RuntimeBackend.OPENHANDS
    assert approvals[0].status == ApprovalStatus.PENDING

    await service.approve(approvals[0].id, "approved for test")
    await service.resume_run(first.id)
    from synode.runtime.worker import RunWorker

    assert await RunWorker(service, worker_id="openhands-approval-worker").run_once() is True
    resumed = await service.get_run(first.id)

    assert resumed.status == RunStatus.COMPLETED
    assert client.accepted == ["conv-1"]


async def test_openhands_http_client_uses_agent_server_api(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    settings.openhands_base_url = "http://openhands.test"
    settings.openhands_api_key = "session-key"
    settings.openhands_api_mode = "agent_server"
    seen: list[tuple[str, str, dict[str, object]]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode()) if request.content else {}
        seen.append((request.method, request.url.path, body))
        assert request.headers.get("X-Session-API-Key") == "session-key"
        if request.url.path == "/health":
            return httpx.Response(200, json={"status": "ok"})
        if request.url.path == "/api/conversations/count":
            return httpx.Response(200, json={"count": 0})
        if request.url.path == "/api/conversations":
            return httpx.Response(200, json={"id": "conv-1", "execution_status": "idle"})
        if request.url.path == "/api/conversations/conv-1/run":
            return httpx.Response(200, json={"success": True})
        if request.url.path == "/api/conversations/conv-1/events/respond_to_confirmation":
            return httpx.Response(200, json={"success": True})
        return httpx.Response(404, json={"detail": "not found"})

    _patch_execution_async_client(monkeypatch, httpx.MockTransport(handle))

    client = HttpOpenHandsClient(settings)
    available, detail = await client.status()
    state = await client.start_conversation(
        {
            "initial_message": {"content": [{"type": "text", "text": "inspect"}]},
            "workspace": "/workspace/project",
        }
    )
    await client.respond_to_confirmation("conv-1", accept=True, reason="approved")

    assert available is True
    assert detail == "OpenHands agent_server endpoint is reachable"
    assert state.conversation_id == "conv-1"
    create_payload = next(body for method, path, body in seen if method == "POST" and path == "/api/conversations")
    assert create_payload["workspace"] == {"working_dir": "/workspace/project"}
    assert create_payload["confirmation_policy"] == {"kind": "AlwaysConfirm"}
    assert ("POST", "/api/conversations/conv-1/run", {}) in seen


async def test_secret_creation_requires_configured_key(service) -> None:
    with pytest.raises(RuntimeError, match="SYNODE_SECRETS_KEY"):
        await service.create_secret(SecretCreateRequest(name="test", value="secret"))


async def _openhands_service(settings, database, client: "_FakeOpenHandsClient") -> OrchestrationService:
    settings.openhands_enabled = True
    settings.openhands_base_url = "http://openhands.test"
    settings.openhands_poll_interval_seconds = 0.01
    roles = RoleRegistry.load_builtin()
    models = ModelProviderRegistry()
    tools = await build_tool_registry(settings, include_mcp=False)
    return OrchestrationService(
        settings,
        database,
        roles,
        models,
        tools,
        run_queue=InMemoryRunQueueTransport(),
        execution_backends=ExecutionBackendRegistry(settings, database, openhands_client=client),
    )


class _FakeOpenHandsClient:
    def __init__(self, statuses: list[str]):
        self.statuses = statuses
        self.accepted: list[str] = []

    async def status(self) -> tuple[bool, str | None]:
        return True, "fake OpenHands ready"

    async def start_conversation(self, payload: dict) -> OpenHandsConversationState:
        return self._state()

    async def get_conversation(self, conversation_id: str) -> OpenHandsConversationState:
        return self._state()

    async def respond_to_confirmation(
        self,
        conversation_id: str,
        *,
        accept: bool,
        reason: str | None = None,
    ) -> None:
        if accept:
            self.accepted.append(conversation_id)

    async def cancel_conversation(self, conversation_id: str) -> None:
        return None

    def _state(self) -> OpenHandsConversationState:
        status = self.statuses.pop(0) if self.statuses else "completed"
        return OpenHandsConversationState(
            conversation_id="conv-1",
            status=status,
            raw={"status": status, "summary": "OpenHands completed the coder node."},
            pending_actions=[
                {"id": "action-1", "tool_name": "terminal", "command": "pytest"}
            ]
            if status == "waiting_for_confirmation"
            else [],
            final_message="OpenHands completed the coder node." if status == "completed" else None,
        )


def _patch_execution_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    class ClientFactory:
        def __init__(self, *args: object, **kwargs: object):
            kwargs["transport"] = transport
            self._client = original(*args, **kwargs)

        async def __aenter__(self) -> httpx.AsyncClient:
            return await self._client.__aenter__()

        async def __aexit__(self, *args: object) -> None:
            await self._client.__aexit__(*args)

    monkeypatch.setattr("synode.runtime.execution.httpx.AsyncClient", ClientFactory)
