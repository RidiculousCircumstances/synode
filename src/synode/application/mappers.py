from __future__ import annotations

from typing import Any

from synode.domain.models import (
    AgentGraphNode,
    AgentGraphNodeEdge,
    AgentGraphResponse,
    AgentRoleResponse,
    InteractionMode,
    MCPServerResponse,
    MCPServerTransport,
    ModelProfileResponse,
    ModelProviderType,
    OperatorRequestKind,
    OperatorRequestResponse,
    OperatorRequestStatus,
    RunMode,
    RunResponse,
    RunStatus,
    SecretResponse,
    ThreadMessageAuthorType,
    ThreadMessageResponse,
    ThreadMessageType,
    ThreadResponse,
    ThreadStatus,
)
from synode.domain.runtime.loop_policy import normalize_native_loop_mode


def to_run_response(run: Any) -> RunResponse:
    return RunResponse(
        id=run.id,
        thread_id=run.thread_id,
        status=RunStatus(run.status),
        mode=RunMode(run.mode),
        interaction_mode=InteractionMode(run.interaction_mode),
        task=run.task,
        workspace=run.workspace,
        model_provider=run.model_provider,
        default_model_profile_id=run.default_model_profile_id,
        role_model_profile_ids={str(key): str(value) for key, value in (run.role_model_profile_ids or {}).items()},
        agent_graph_id=run.agent_graph_id,
        agent_graph_snapshot=run.agent_graph_snapshot or {},
        observability_trace_id=run.observability_trace_id,
        final_answer=run.final_answer,
        error=run.error,
        worker_id=run.worker_id,
        queued_at=run.queued_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        heartbeat_at=run.heartbeat_at,
        created_at=run.created_at,
        updated_at=run.updated_at,
    )


def to_operator_request_response(record: Any) -> OperatorRequestResponse:
    return OperatorRequestResponse(
        id=record.id,
        run_id=record.run_id,
        thread_id=record.thread_id,
        node_id=record.node_id,
        role=record.role,
        kind=OperatorRequestKind(record.kind),
        prompt=record.prompt,
        context=record.context or {},
        proposed_payload=record.proposed_payload or {},
        status=OperatorRequestStatus(record.status),
        response_payload=record.response_payload or {},
        created_at=record.created_at,
        resolved_at=record.resolved_at,
        cancelled_at=record.cancelled_at,
        consumed_at=record.consumed_at,
    )


def to_secret_response(secret: Any) -> SecretResponse:
    return SecretResponse(
        id=secret.id,
        name=secret.name,
        secret_set=bool(secret.encrypted_value),
        created_at=secret.created_at,
        updated_at=secret.updated_at,
    )


def to_model_profile_response(profile: Any) -> ModelProfileResponse:
    return ModelProfileResponse(
        id=profile.id,
        name=profile.name,
        provider_type=ModelProviderType(profile.provider_type),
        base_url=profile.base_url,
        model=profile.model,
        options=profile.options or {},
        secret_id=profile.secret_id,
        secret_set=profile.secret_id is not None,
        enabled=profile.enabled,
        created_at=profile.created_at,
        updated_at=profile.updated_at,
    )


def to_mcp_server_response(server: Any) -> MCPServerResponse:
    return MCPServerResponse(
        id=server.id,
        name=server.name,
        transport=MCPServerTransport(server.transport),
        config=server.config or {},
        enabled=server.enabled,
        tools=list(server.tools or []),
        last_error=server.last_error,
        last_discovered_at=server.last_discovered_at,
        created_at=server.created_at,
        updated_at=server.updated_at,
    )


def to_agent_role_response(role: Any) -> AgentRoleResponse:
    return AgentRoleResponse(
        id=role.id,
        name=role.name,
        mission=role.mission,
        non_goals=role.non_goals or [],
        allowed_tools=role.allowed_tools or [],
        requires_approval_for=role.requires_approval_for or [],
        output_contract=role.output_contract,
        builtin=role.builtin,
        enabled=role.enabled,
        created_at=role.created_at,
        updated_at=role.updated_at,
    )


def to_agent_graph_response(graph: Any) -> AgentGraphResponse:
    return AgentGraphResponse(
        id=graph.id,
        name=graph.name,
        graph_schema_version=graph.graph_schema_version,
        nodes=[AgentGraphNode.model_validate(node) for node in (graph.nodes or [])],
        node_edges=[AgentGraphNodeEdge.model_validate(edge) for edge in (graph.node_edges or [])],
        default_model_profile_id=graph.default_model_profile_id,
        role_model_profile_ids={str(key): str(value) for key, value in (graph.role_model_profile_ids or {}).items()},
        node_runtime_bindings={str(key): str(value) for key, value in (graph.node_runtime_bindings or {}).items()},
        node_contracts={str(key): str(value) for key, value in (graph.node_contracts or {}).items()},
        node_loop_policies={
            str(key): normalize_native_loop_mode(value)
            for key, value in (graph.node_loop_policies or {}).items()
        },
        is_default=graph.is_default,
        enabled=graph.enabled,
        created_at=graph.created_at,
        updated_at=graph.updated_at,
    )


def to_thread_message_response(message: Any) -> ThreadMessageResponse:
    return ThreadMessageResponse(
        id=message.id,
        thread_id=message.thread_id,
        run_id=message.run_id,
        author_type=ThreadMessageAuthorType(message.author_type),
        author_name=message.author_name,
        message_type=ThreadMessageType(message.message_type),
        content=message.content,
        metadata=message.metadata_,
        created_at=message.created_at,
    )


def to_thread_response(
    thread: Any,
    latest_run: Any | None = None,
    latest_message: Any | None = None,
) -> ThreadResponse:
    return ThreadResponse(
        id=thread.id,
        title=thread.title,
        status=ThreadStatus(thread.status),
        latest_run_id=latest_run.id if latest_run else None,
        latest_run_status=RunStatus(latest_run.status) if latest_run else None,
        last_message=latest_message.content if latest_message else None,
        created_at=thread.created_at,
        updated_at=thread.updated_at,
    )
