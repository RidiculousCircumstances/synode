from __future__ import annotations

import json
import pathlib

import httpx
import pytest

from synode.application.orchestration import OrchestrationService
from synode.application.ports import NodeExecutionInput
from synode.domain.models import (
    AgentGraphCreateRequest,
    ApprovalStatus,
    ModelProfileCreateRequest,
    ModelProviderType,
    RunMode,
    RunStatus,
    RuntimeBackend,
    SecretCreateRequest,
)
from synode.domain.roles import RoleRegistry
from synode.infrastructure.composition import InfrastructureMCPToolManager
from synode.infrastructure.models.provider import ModelProviderRegistry
from synode.infrastructure.observability import Observability
from synode.infrastructure.persistence.repository import Repository
from synode.infrastructure.runtime.execution import (
    ExecutionBackendRegistry,
    HttpOpenHandsClient,
    OpenHandsConversationState,
    OpenHandsNodeBackend,
    _conversation_payload,
)
from synode.infrastructure.runtime.queue import InMemoryRunQueueTransport
from synode.infrastructure.security import SecretCipher
from synode.infrastructure.tools import ToolExecutor, build_tool_registry
from synode.infrastructure.tools.sandbox import SandboxRunner


def _graph_payload(
    roles: dict[str, object],
    *,
    name: str,
    workers: list[str],
    node_runtime_bindings: dict[str, RuntimeBackend] | None = None,
    node_contracts: dict[str, str] | None = None,
    node_loop_policies: dict[str, str] | None = None,
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
        node_loop_policies=node_loop_policies or {},
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


async def test_default_configuration_syncs_existing_builtin_roles(settings, database) -> None:
    builtin_roles = RoleRegistry.load_builtin()
    async with database.session() as session:
        repo = Repository(session)
        await repo.create_agent_role(
            name="coder",
            mission="old coder mission",
            allowed_tools=["native.fs_read"],
            builtin=True,
        )
        await repo.ensure_default_configuration(
            builtin_roles=builtin_roles.as_public(),
            ollama_base_url=settings.ollama_base_url,
            ollama_model=settings.ollama_model,
        )
        coder = await repo.get_agent_role_by_name("coder")

    assert coder is not None
    assert coder.mission == builtin_roles.get("coder").mission
    assert "native.fs_list" in coder.allowed_tools
    assert coder.builtin is True


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


async def test_graph_runtime_bindings_accept_control_roles_when_contract_supported(service) -> None:
    roles = {role.name: role for role in await service.list_agent_roles()}

    graph = await service.create_agent_graph(
        _graph_payload(
            roles,
            name="control runtime graph",
            workers=["coder"],
            node_runtime_bindings={"reviewer": RuntimeBackend.OPENHANDS},
        )
    )

    assert graph.node_runtime_bindings["reviewer"] == RuntimeBackend.OPENHANDS
    assert graph.node_contracts["reviewer"] == "reviewer_decision"


async def test_agent_graph_loop_policies_are_persisted_and_resolved_in_snapshot(
    service,
    tmp_path: pathlib.Path,
) -> None:
    profile = await service.create_model_profile(
        ModelProfileCreateRequest(
            name="strict loop profile",
            provider_type=ModelProviderType.FAKE,
            model="fake",
            options={"native_loop_mode": "strict"},
        )
    )
    roles = {role.name: role for role in await service.list_agent_roles()}
    graph = await service.create_agent_graph(
        _graph_payload(
            roles,
            name="loop policy graph",
            workers=["coder"],
            default_model_profile_id=profile.id,
            node_loop_policies={"coder": "autonomous"},
        )
    )

    run = await service.create_run(
        "Inspect loop policy resolution.",
        workspace=str(tmp_path),
        mode=RunMode.CODING,
        agent_graph_id=graph.id,
    )

    assert graph.node_loop_policies == {"coder": "autonomous"}
    assert run.agent_graph_snapshot["node_loop_policies"]["coder"] == "autonomous"
    assert run.agent_graph_snapshot["node_loop_policies"]["supervisor"] == "strict"
    assert run.agent_graph_snapshot["nodes"][1]["native_loop_mode"] == "autonomous"


async def test_agent_graph_loop_policies_reject_unknown_mode(service, database) -> None:
    roles = {role.name: role for role in await service.list_agent_roles()}
    payload = _graph_payload(
        roles,
        name="bad loop policy graph",
        workers=["coder"],
    )

    with pytest.raises(ValueError, match="unknown native loop mode"):
        async with database.session() as session:
            await Repository(session).create_agent_graph(
                name=payload.name,
                nodes=[node.model_dump(mode="json") for node in payload.nodes],
                node_edges=[edge.model_dump(mode="json") for edge in payload.node_edges],
                node_loop_policies={"coder": "careful"},
            )


async def test_openhands_node_backend_completes_coding_run(settings, database, tmp_path: pathlib.Path) -> None:
    client = _FakeOpenHandsClient(["completed"])
    service = await _openhands_service(settings, database, client)
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
    assert len(client.payloads) == 1
    assert any(artifact.kind == "openhands_coder" for artifact in artifacts)
    proxy_config = client.payloads[0]["agent_settings"]["mcp_config"]["mcpServers"]["synode"]
    assert "/mcp/proxy/" in proxy_config["url"]
    assert proxy_config["type"] == "streamable-http"
    assert "tools" not in proxy_config
    assert proxy_config["headers"]["Authorization"].startswith("Bearer ")


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


async def test_openhands_control_nodes_use_contract_payloads(
    settings,
    database,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n", encoding="utf-8")
    client = _FakeOpenHandsClient(["completed", "completed"])
    service = await _openhands_service(settings, database, client)
    roles = {role.name: role for role in await service.list_agent_roles()}
    graph = await service.create_agent_graph(
        _graph_payload(
            roles,
            name="openhands control graph",
            workers=["data_analyst"],
            node_runtime_bindings={
                "supervisor": RuntimeBackend.OPENHANDS,
                "reviewer": RuntimeBackend.OPENHANDS,
            },
        )
    )

    result = await service.run_task(
        "Analyze data with external control nodes",
        workspace=str(tmp_path),
        model_provider="fake",
        agent_graph_id=graph.id,
    )
    async with database.session() as session:
        states = await Repository(session).list_runtime_node_states(result.id)

    assert result.status == RunStatus.COMPLETED
    assert result.agent_graph_snapshot["node_runtime_bindings"]["supervisor"] == RuntimeBackend.OPENHANDS
    assert result.agent_graph_snapshot["node_runtime_bindings"]["reviewer"] == RuntimeBackend.OPENHANDS
    assert any(state.node_id == "supervisor" and state.contract_id == "supervisor_decision" for state in states)
    assert any(state.node_id == "reviewer" and state.contract_id == "reviewer_decision" for state in states)


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
    from synode.infrastructure.runtime.worker import RunWorker

    assert await RunWorker(service, worker_id="openhands-approval-worker").run_once() is True
    resumed = await service.get_run(first.id)

    assert resumed.status == RunStatus.COMPLETED
    assert client.accepted == ["conv-1"]


async def test_openhands_contract_repair_continues_same_conversation(
    settings,
    database,
    tmp_path: pathlib.Path,
) -> None:
    client = _FakeOpenHandsClient(["completed", "completed"], invalid_completions=1)
    service = await _openhands_service(settings, database, client)
    roles = {role.name: role for role in await service.list_agent_roles()}
    graph = await service.create_agent_graph(
        _graph_payload(
            roles,
            name="openhands repair graph",
            workers=["coder"],
            node_runtime_bindings={"coder": RuntimeBackend.OPENHANDS},
        )
    )

    result = await service.run_task(
        "Let OpenHands repair its malformed contract output",
        workspace=str(tmp_path),
        model_provider="fake",
        mode=RunMode.CODING,
        agent_graph_id=graph.id,
    )
    artifacts = await service.list_artifacts(result.id, limit=20)

    assert result.status == RunStatus.COMPLETED
    assert client.messages and "Synode rejected your previous final response" in client.messages[0]
    assert any(
        artifact.kind == "openhands_coder" and artifact.content.get("status") == "contract_error"
        for artifact in artifacts
    )
    assert any(
        artifact.kind == "openhands_coder" and artifact.content.get("status") == "completed"
        for artifact in artifacts
    )


async def test_openhands_rejects_ungrounded_real_mcp_report(settings, database) -> None:
    settings.openhands_enabled = True
    settings.openhands_base_url = "http://openhands.test"
    settings.openhands_contract_repair_attempts = 0
    client = _FakeOpenHandsClient(["completed"])
    backend = OpenHandsNodeBackend(settings, database, client=client)

    with pytest.raises(RuntimeError, match="not grounded in Synode MCP tool audit"):
        await backend.execute(
            NodeExecutionInput(
                run_id="run-ungrounded",
                thread_id="thread-ungrounded",
                node_id="coder",
                role="coder",
                backend_id=RuntimeBackend.OPENHANDS.value,
                contract_id="worker_agent_output",
                task="Patch the repository",
                workspace="/workspace/project",
                mode=RunMode.CODING.value,
                model_provider=ModelProviderType.OLLAMA.value,
                tool_proxy_url="http://127.0.0.1:8787/mcp/proxy/test",
                tool_proxy_token="token",
                tool_proxy_tools=["native.fs_list"],
            )
        )


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
        if request.url.path == "/api/conversations/conv-1":
            return httpx.Response(200, json={"id": "conv-1", "execution_status": "finished"})
        if request.url.path == "/api/conversations/conv-1/agent_final_response":
            return httpx.Response(200, json={"response": '{"summary":"done","changed_files":[]}'})
        if request.url.path == "/api/conversations/conv-1/events":
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
            "metadata": {"synode_run_id": "run-1", "ignored": None},
            "agent_settings": {
                "schema_version": 4,
                "agent_kind": "openhands",
                "agent": "CodeActAgent",
                "llm": {"model": "ollama/qwen2.5-coder:7b", "api_key": "ollama"},
            },
        }
    )
    finished = await client.get_conversation("conv-1")
    repaired = await client.send_message("conv-1", "repair")
    await client.respond_to_confirmation("conv-1", accept=True, reason="approved")

    assert available is True
    assert detail == "OpenHands agent_server endpoint is reachable"
    assert state.conversation_id == "conv-1"
    assert finished.final_message == '{"summary":"done","changed_files":[]}'
    assert repaired.conversation_id == "conv-1"
    create_payload = next(body for method, path, body in seen if method == "POST" and path == "/api/conversations")
    repair_payload = next(body for method, path, body in seen if method == "POST" and path == "/api/conversations/conv-1/events")
    assert create_payload["workspace"] == {"working_dir": "/workspace/project", "kind": "LocalWorkspace"}
    assert create_payload["confirmation_policy"] == {"kind": "AlwaysConfirm"}
    assert create_payload["observability_metadata"] == {"synode_run_id": "run-1"}
    assert "metadata" not in create_payload
    assert create_payload["initial_message"]["run"] is True
    assert create_payload["agent_settings"]["agent"] == "CodeActAgent"
    assert repair_payload == {"role": "user", "run": True, "content": [{"type": "text", "text": "repair"}]}
    assert ("POST", "/api/conversations/conv-1/run", {}) not in seen


async def test_openhands_http_client_treats_fallback_already_running_start_as_success(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.openhands_base_url = "http://openhands.test"
    settings.openhands_api_mode = "agent_server"
    seen: list[tuple[str, str]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == "/api/conversations":
            return httpx.Response(201, json={"id": "conv-1", "execution_status": "idle"})
        if request.url.path == "/api/conversations/conv-1/run":
            return httpx.Response(
                409,
                json={"detail": "Conversation already running. Wait for completion or pause first."},
            )
        if request.url.path == "/api/conversations/conv-1":
            return httpx.Response(200, json={"id": "conv-1", "execution_status": "running"})
        return httpx.Response(404, json={"detail": "not found"})

    _patch_execution_async_client(monkeypatch, httpx.MockTransport(handle))

    client = HttpOpenHandsClient(settings)
    state = await client.start_conversation(
        {
            "workspace": "/workspace/project",
            "agent_settings": {
                "schema_version": 4,
                "agent_kind": "openhands",
                "agent": "CodeActAgent",
                "llm": {"model": "ollama/qwen2.5-coder:7b", "api_key": "ollama"},
            },
        }
    )

    assert state.conversation_id == "conv-1"
    assert state.status == "running"
    assert ("POST", "/api/conversations/conv-1/run") in seen
    assert ("GET", "/api/conversations/conv-1") in seen


async def test_openhands_http_client_rejects_idle_state_after_start_conflict(
    settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings.openhands_base_url = "http://openhands.test"
    settings.openhands_api_mode = "agent_server"

    def handle(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/conversations":
            return httpx.Response(201, json={"id": "conv-1", "execution_status": "idle"})
        if request.url.path == "/api/conversations/conv-1/run":
            return httpx.Response(
                409,
                json={"detail": "Conversation already running. Wait for completion or pause first."},
            )
        if request.url.path == "/api/conversations/conv-1":
            return httpx.Response(200, json={"id": "conv-1", "execution_status": "idle"})
        return httpx.Response(404, json={"detail": "not found"})

    _patch_execution_async_client(monkeypatch, httpx.MockTransport(handle))

    client = HttpOpenHandsClient(settings)
    with pytest.raises(RuntimeError, match="conversation is still idle"):
        await client.start_conversation(
            {
                "workspace": "/workspace/project",
                "agent_settings": {
                    "schema_version": 4,
                    "agent_kind": "openhands",
                    "agent": "CodeActAgent",
                    "llm": {"model": "ollama/qwen2.5-coder:7b", "api_key": "ollama"},
                },
            }
        )


async def test_openhands_payload_uses_synode_profile_and_workspace_mapping(settings, database) -> None:
    settings.openhands_max_iterations = 12
    settings.openhands_host_workspace = "/host/synode-workspaces"
    settings.openhands_container_workspace = "/workspace"
    async with database.session() as session:
        profile = await Repository(session).create_model_profile(
            name="openhands eval profile",
            provider_type=ModelProviderType.OLLAMA,
            base_url="http://127.0.0.1:11434",
            model="qwen2.5-coder:7b",
            options={"temperature": 0.1, "top_p": 0.9, "num_predict": 800, "timeout_seconds": 180},
        )

    payload = await _conversation_payload(
        NodeExecutionInput(
            run_id="run-1",
            thread_id="thread-1",
            node_id="coder",
            role="coder",
            backend_id=RuntimeBackend.OPENHANDS.value,
            contract_id="worker_agent_output",
            task="Fix the CLI",
            workspace="/workspace/evals/task",
            mode=RunMode.CODING.value,
            plan_task="Inspect the CLI entrypoint.",
            plan_steps=[
                {"role": "coder", "task": "Inspect the CLI entrypoint.", "tool_calls": []},
                {"role": "coder", "task": "Patch argument parsing and run tests.", "tool_calls": []},
                {"role": "reviewer", "task": "Review the result.", "tool_calls": []},
            ],
            default_model_profile_id=profile.id,
        ),
        settings,
        database,
    )

    llm = payload["agent_settings"]["llm"]
    message_text = payload["initial_message"]["content"][0]["text"]
    assert payload["workspace"] == "/host/synode-workspaces/evals/task"
    assert payload["max_iterations"] == 12
    assert "Full user task: Fix the CLI" in message_text
    assert "Role plan steps:" in message_text
    assert "Patch argument parsing and run tests." in message_text
    assert "Review the result." not in message_text
    assert "When this node's work is complete" in message_text
    assert llm["model"] == "ollama/qwen2.5-coder:7b"
    assert llm["base_url"] == "http://127.0.0.1:11434"
    assert llm["temperature"] == 0.1
    assert llm["top_p"] == 0.9
    assert llm["max_output_tokens"] == 800
    assert llm["timeout"] == 180
    assert llm["reasoning_effort"] == "none"
    assert llm["enable_encrypted_reasoning"] is False
    assert "native_tool_calling" not in llm


async def test_openhands_llm_settings_can_override_native_tool_calling(settings, database) -> None:
    async with database.session() as session:
        profile = await Repository(session).create_model_profile(
            name="openhands eval profile no native tools",
            provider_type=ModelProviderType.OLLAMA,
            base_url="http://127.0.0.1:11434",
            model="qwen2.5-coder:7b",
            options={"native_tool_calling": False},
        )

    payload = await _conversation_payload(
        NodeExecutionInput(
            run_id="run-1",
            thread_id="thread-1",
            node_id="coder",
            role="coder",
            backend_id=RuntimeBackend.OPENHANDS.value,
            contract_id="worker_agent_output",
            task="Fix the CLI",
            workspace="/workspace/evals/task",
            mode=RunMode.CODING.value,
            default_model_profile_id=profile.id,
        ),
        settings,
        database,
    )

    assert payload["agent_settings"]["llm"]["native_tool_calling"] is False


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
    observability = Observability(settings)
    tool_executor_factory = lambda role_registry: ToolExecutor(  # noqa: E731
        database,
        role_registry,
        tools,
        settings,
        observability,
    )
    return OrchestrationService(
        settings=settings,
        database=database,
        roles=roles,
        models=models,
        tools=tools,
        observability=observability,
        run_queue=InMemoryRunQueueTransport(),
        execution_backends=ExecutionBackendRegistry(settings, database, openhands_client=client),
        secret_cipher=SecretCipher(settings) if settings.secrets_key else None,
        tool_executor=tool_executor_factory(roles),
        tool_executor_factory=tool_executor_factory,
        repository_factory=Repository,
        sandbox_status_factory=lambda: SandboxRunner(settings).status(),
        mcp_tool_manager=InfrastructureMCPToolManager(),
    )


class _FakeOpenHandsClient:
    def __init__(self, statuses: list[str], *, invalid_completions: int = 0):
        self.statuses = statuses
        self.invalid_completions = invalid_completions
        self.accepted: list[str] = []
        self.messages: list[str] = []
        self.contract_id = "worker_agent_output"
        self.payloads: list[dict] = []

    async def status(self) -> tuple[bool, str | None]:
        return True, "fake OpenHands ready"

    async def start_conversation(self, payload: dict) -> OpenHandsConversationState:
        self.payloads.append(payload)
        metadata = payload.get("metadata", {})
        if isinstance(metadata, dict):
            self.contract_id = str(metadata.get("synode_contract_id") or self.contract_id)
        return self._state()

    async def get_conversation(self, conversation_id: str) -> OpenHandsConversationState:
        return self._state()

    async def send_message(self, conversation_id: str, text: str) -> OpenHandsConversationState:
        self.messages.append(text)
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
        if status == "completed" and self.invalid_completions > 0:
            self.invalid_completions -= 1
            invalid = '```json\n{"tool_name":"native.fs_list","ok":true}\n```'
            return OpenHandsConversationState(
                conversation_id="conv-1",
                status=status,
                raw={"status": status, "final_message": invalid},
                final_message=invalid,
            )
        contract_payload = self._contract_payload()
        return OpenHandsConversationState(
            conversation_id="conv-1",
            status=status,
            raw={
                "status": status,
                "summary": "OpenHands completed the Synode node.",
                "synode_payload": contract_payload,
            },
            pending_actions=[
                {"id": "action-1", "tool_name": "terminal", "command": "pytest"}
            ]
            if status == "waiting_for_confirmation"
            else [],
            final_message=json.dumps(contract_payload) if status == "completed" else None,
        )

    def _contract_payload(self) -> dict:
        if self.contract_id == "supervisor_decision":
            return {
                "selected_roles": ["data_analyst"],
                "plan": [{"role": "data_analyst", "task": "Analyze the dataset.", "tool_calls": []}],
                "confidence": "high",
                "risk_level": "analysis",
                "reasoning_summary": "Fake OpenHands supervisor decision.",
            }
        if self.contract_id == "reviewer_decision":
            return {
                "verdict": "proceed",
                "blockers": [],
                "advisory_risks": [],
                "missing_evidence": [],
                "required_next_actions": [],
                "confidence": "high",
            }
        return {
            "role": "coder",
            "summary": "OpenHands completed the coder node.",
            "tool_results": [],
            "risks": [],
        }


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

    monkeypatch.setattr("synode.infrastructure.runtime.execution.httpx.AsyncClient", ClientFactory)
