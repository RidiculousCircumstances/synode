from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse

from synode.config import Settings
from synode.runtime.service import OrchestrationService, create_service
from synode.schemas import (
    AgentGraphCreateRequest,
    AgentGraphResponse,
    AgentGraphUpdateRequest,
    AgentRoleCreateRequest,
    AgentRoleResponse,
    AgentRoleUpdateRequest,
    ApprovalDecision,
    ApprovalResponse,
    ApprovalStatus,
    ArtifactResponse,
    MCPServerCreateRequest,
    MCPServerResponse,
    MCPServerUpdateRequest,
    ModelProfileCreateRequest,
    ModelProfileResponse,
    ModelProfileTestResponse,
    ModelProfileUpdateRequest,
    OperatorRequestDecision,
    OperatorRequestResponse,
    OperatorRequestStatus,
    RunCreateRequest,
    RunEventResponse,
    RunMetricsResponse,
    RunMode,
    RunResponse,
    RunStatus,
    RunStopRequest,
    RuntimeStatusResponse,
    SandboxStatusResponse,
    SecretCreateRequest,
    SecretResponse,
    SecretUpdateRequest,
    SystemMetricsResponse,
    ThreadCreateRequest,
    ThreadDetailResponse,
    ThreadMessageResponse,
    ThreadResponse,
    ThreadRunCreateRequest,
    ThreadStatus,
    ThreadUpdateRequest,
    ToolAuditResponse,
)


def _page_limit(value: int, maximum: int = 200) -> int:
    return max(1, min(value, maximum))


def _page_offset(value: int) -> int:
    return max(0, value)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    service = await create_service(settings)
    app.state.service = service
    try:
        yield
    finally:
        await service.close()


def create_app() -> FastAPI:
    settings = Settings()
    app = FastAPI(title="Synode", version="0.1.0", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.api_cors_origin_list,
        allow_origin_regex=settings.api_cors_origin_regex,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/threads", response_model=ThreadDetailResponse)
    async def create_thread(payload: ThreadCreateRequest, request: Request) -> ThreadDetailResponse:
        service: OrchestrationService = request.app.state.service
        try:
            detail = await service.create_thread(
                message=payload.message,
                title=payload.title,
                workspace=payload.workspace,
                model_provider=payload.model_provider,
                mode=payload.mode,
                default_model_profile_id=payload.default_model_profile_id,
                role_model_profile_ids=payload.role_model_profile_ids,
                agent_graph_id=payload.agent_graph_id,
                interaction_mode=payload.interaction_mode,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        if detail.thread.latest_run_id is None:
            raise HTTPException(status_code=500, detail="thread was created without a run")
        try:
            await service.start_run(detail.thread.latest_run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await service.get_thread(detail.thread.id)

    @app.get("/threads", response_model=list[ThreadResponse])
    async def list_threads(
        request: Request,
        status: ThreadStatus | None = ThreadStatus.ACTIVE,
        search: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ThreadResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_threads(
            status=status,
            search=search,
            limit=_page_limit(limit),
            offset=_page_offset(offset),
        )

    @app.get("/threads/{thread_id}", response_model=ThreadDetailResponse)
    async def get_thread(
        thread_id: str,
        request: Request,
        runs_limit: int = 50,
        runs_offset: int = 0,
        messages_limit: int = 200,
        messages_offset: int = 0,
    ) -> ThreadDetailResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.get_thread(
                thread_id,
                runs_limit=_page_limit(runs_limit),
                runs_offset=_page_offset(runs_offset),
                messages_limit=_page_limit(messages_limit, maximum=500),
                messages_offset=_page_offset(messages_offset),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/threads/{thread_id}", response_model=ThreadResponse)
    async def update_thread(
        thread_id: str, payload: ThreadUpdateRequest, request: Request
    ) -> ThreadResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.update_thread_title(thread_id, payload.title)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/threads/{thread_id}/archive", response_model=ThreadResponse)
    async def archive_thread(thread_id: str, request: Request) -> ThreadResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.archive_thread(thread_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/threads/{thread_id}/messages", response_model=list[ThreadMessageResponse])
    async def list_thread_messages(
        thread_id: str,
        request: Request,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ThreadMessageResponse]:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.list_thread_messages(
                thread_id,
                limit=_page_limit(limit, maximum=500),
                offset=_page_offset(offset),
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/threads/{thread_id}/runs", response_model=RunResponse)
    async def create_thread_run(
        thread_id: str, payload: ThreadRunCreateRequest, request: Request
    ) -> RunResponse:
        service: OrchestrationService = request.app.state.service
        try:
            run = await service.create_thread_run(
                thread_id=thread_id,
                message=payload.message,
                workspace=payload.workspace,
                model_provider=payload.model_provider,
                mode=payload.mode,
                default_model_profile_id=payload.default_model_profile_id,
                role_model_profile_ids=payload.role_model_profile_ids,
                agent_graph_id=payload.agent_graph_id,
                interaction_mode=payload.interaction_mode,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        try:
            await service.start_run(run.id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await service.get_run(run.id)

    @app.post("/runs", response_model=RunResponse)
    async def create_run(payload: RunCreateRequest, request: Request) -> RunResponse:
        service: OrchestrationService = request.app.state.service
        try:
            run = await service.create_run(
                task=payload.task,
                workspace=payload.workspace,
                model_provider=payload.model_provider,
                mode=payload.mode,
                default_model_profile_id=payload.default_model_profile_id,
                role_model_profile_ids=payload.role_model_profile_ids,
                agent_graph_id=payload.agent_graph_id,
                interaction_mode=payload.interaction_mode,
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            await service.start_run(run.id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return await service.get_run(run.id)

    @app.get("/runs", response_model=list[RunResponse])
    async def list_runs(
        request: Request,
        status: RunStatus | None = None,
        mode: RunMode | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_runs(
            status=status,
            mode=mode,
            limit=_page_limit(limit),
            offset=_page_offset(offset),
        )

    @app.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str, request: Request) -> RunResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.get_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/events", response_model=list[RunEventResponse])
    async def get_events(
        run_id: str,
        request: Request,
        after_id: int = 0,
        limit: int = 200,
    ) -> list[RunEventResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_event_responses(run_id, after_id=after_id, limit=_page_limit(limit, maximum=500))

    @app.get("/runs/{run_id}/events/stream")
    async def stream_events(run_id: str, request: Request, after_id: int = 0) -> StreamingResponse:
        service: OrchestrationService = request.app.state.service

        async def generator() -> AsyncIterator[str]:
            header_last_id = request.headers.get("last-event-id")
            cursor = int(header_last_id) if header_last_id and header_last_id.isdigit() else after_id
            while True:
                events = await service.list_event_responses(run_id, after_id=cursor, limit=200)
                for event in events:
                    cursor = event.id
                    yield (
                        f"id: {event.id}\n"
                        f"event: {event.event_type}\n"
                        f"data: {event.model_dump_json()}\n\n"
                    )
                run = await service.get_run(run_id)
                if run.status.value in {
                    "completed",
                    "failed",
                    "failed_verification",
                    "waiting_approval",
                    "waiting_operator",
                    "cancelled",
                }:
                    break
                if not events:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(generator(), media_type="text/event-stream")

    @app.get("/runs/{run_id}/artifacts", response_model=list[ArtifactResponse])
    async def get_artifacts(
        run_id: str,
        request: Request,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ArtifactResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_artifacts(run_id, limit=_page_limit(limit), offset=_page_offset(offset))

    @app.get("/runs/{run_id}/tool-audit", response_model=list[ToolAuditResponse])
    async def get_tool_audit(
        run_id: str,
        request: Request,
        limit: int = 200,
        offset: int = 0,
    ) -> list[ToolAuditResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_tool_audit(run_id, limit=_page_limit(limit, maximum=500), offset=_page_offset(offset))

    @app.get("/runs/{run_id}/approvals", response_model=list[ApprovalResponse])
    async def get_run_approvals(
        run_id: str,
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ApprovalResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_approvals(run_id=run_id, limit=_page_limit(limit), offset=_page_offset(offset))

    @app.get("/runs/{run_id}/operator-requests", response_model=list[OperatorRequestResponse])
    async def get_run_operator_requests(
        run_id: str,
        request: Request,
        status: OperatorRequestStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[OperatorRequestResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_operator_requests(
            run_id=run_id,
            status=status,
            limit=_page_limit(limit),
            offset=_page_offset(offset),
        )

    @app.get("/runs/{run_id}/metrics", response_model=RunMetricsResponse)
    async def get_run_metrics(run_id: str, request: Request) -> RunMetricsResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.run_metrics(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/runs/{run_id}/resume")
    async def resume_run(run_id: str, request: Request) -> dict[str, str]:
        service: OrchestrationService = request.app.state.service
        try:
            await service.start_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {"status": "scheduled"}

    @app.post("/runs/{run_id}/stop", response_model=RunResponse)
    async def stop_run(
        run_id: str,
        request: Request,
        payload: RunStopRequest | None = None,
    ) -> RunResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.stop_run(run_id, payload.reason if payload else None)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/approvals/{approval_id}/approve")
    async def approve(approval_id: str, payload: ApprovalDecision, request: Request) -> dict[str, str]:
        service: OrchestrationService = request.app.state.service
        await service.approve(approval_id, payload.reason)
        return {"status": "approved"}

    @app.post("/approvals/{approval_id}/reject")
    async def reject(approval_id: str, payload: ApprovalDecision, request: Request) -> dict[str, str]:
        service: OrchestrationService = request.app.state.service
        await service.reject(approval_id, payload.reason)
        return {"status": "rejected"}

    @app.post("/operator-requests/{request_id}/respond", response_model=OperatorRequestResponse)
    async def respond_operator_request(
        request_id: str,
        payload: OperatorRequestDecision,
        request: Request,
    ) -> OperatorRequestResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.respond_operator_request(request_id, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/operator-requests/{request_id}/cancel", response_model=OperatorRequestResponse)
    async def cancel_operator_request(
        request_id: str,
        payload: ApprovalDecision,
        request: Request,
    ) -> OperatorRequestResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.cancel_operator_request(request_id, payload.reason)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/approvals", response_model=list[ApprovalResponse])
    async def list_approvals(
        request: Request,
        status: ApprovalStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ApprovalResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_approvals(status=status, limit=_page_limit(limit), offset=_page_offset(offset))

    @app.get("/secrets", response_model=list[SecretResponse])
    async def list_secrets(request: Request, limit: int = 50, offset: int = 0) -> list[SecretResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_secrets(limit=_page_limit(limit), offset=_page_offset(offset))

    @app.post("/secrets", response_model=SecretResponse)
    async def create_secret(payload: SecretCreateRequest, request: Request) -> SecretResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.create_secret(payload)
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/secrets/{secret_id}", response_model=SecretResponse)
    async def update_secret(
        secret_id: str,
        payload: SecretUpdateRequest,
        request: Request,
    ) -> SecretResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.update_secret(secret_id, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/model-profiles", response_model=list[ModelProfileResponse])
    async def list_model_profiles(
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ModelProfileResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_model_profiles(limit=_page_limit(limit), offset=_page_offset(offset))

    @app.post("/model-profiles", response_model=ModelProfileResponse)
    async def create_model_profile(
        payload: ModelProfileCreateRequest,
        request: Request,
    ) -> ModelProfileResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.create_model_profile(payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.patch("/model-profiles/{profile_id}", response_model=ModelProfileResponse)
    async def update_model_profile(
        profile_id: str,
        payload: ModelProfileUpdateRequest,
        request: Request,
    ) -> ModelProfileResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.update_model_profile(profile_id, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/model-profiles/{profile_id}/test", response_model=ModelProfileTestResponse)
    async def test_model_profile(profile_id: str, request: Request) -> ModelProfileTestResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.test_model_profile(profile_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/agents", response_model=list[AgentRoleResponse])
    async def agents(request: Request, limit: int = 100, offset: int = 0) -> list[AgentRoleResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_agent_roles(limit=_page_limit(limit, maximum=500), offset=_page_offset(offset))

    @app.post("/agents", response_model=AgentRoleResponse)
    async def create_agent(payload: AgentRoleCreateRequest, request: Request) -> AgentRoleResponse:
        service: OrchestrationService = request.app.state.service
        return await service.create_agent_role(payload)

    @app.patch("/agents/{role_id}", response_model=AgentRoleResponse)
    async def update_agent(
        role_id: str,
        payload: AgentRoleUpdateRequest,
        request: Request,
    ) -> AgentRoleResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.update_agent_role(role_id, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/agent-graphs", response_model=list[AgentGraphResponse])
    async def list_agent_graphs(request: Request, limit: int = 50, offset: int = 0) -> list[AgentGraphResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_agent_graphs(limit=_page_limit(limit), offset=_page_offset(offset))

    @app.post("/agent-graphs", response_model=AgentGraphResponse)
    async def create_agent_graph(
        payload: AgentGraphCreateRequest,
        request: Request,
    ) -> AgentGraphResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.create_agent_graph(payload)
        except (LookupError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/agent-graphs/{graph_id}", response_model=AgentGraphResponse)
    async def update_agent_graph(
        graph_id: str,
        payload: AgentGraphUpdateRequest,
        request: Request,
    ) -> AgentGraphResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.update_agent_graph(graph_id, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/tools")
    async def tools(request: Request) -> dict[str, list[str]]:
        service: OrchestrationService = request.app.state.service
        return {"tools": service.tools.list_names()}

    @app.get("/mcp/tools")
    async def mcp_tools(request: Request) -> dict[str, list[str]]:
        service: OrchestrationService = request.app.state.service
        return {"tools": [name for name in service.tools.list_names() if name.startswith("mcp.")]}

    @app.get("/mcp/servers", response_model=list[MCPServerResponse])
    async def list_mcp_servers(
        request: Request,
        limit: int = 50,
        offset: int = 0,
    ) -> list[MCPServerResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_mcp_servers(limit=_page_limit(limit), offset=_page_offset(offset))

    @app.post("/mcp/servers", response_model=MCPServerResponse)
    async def create_mcp_server(payload: MCPServerCreateRequest, request: Request) -> MCPServerResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.create_mcp_server(payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.patch("/mcp/servers/{server_id}", response_model=MCPServerResponse)
    async def update_mcp_server(
        server_id: str,
        payload: MCPServerUpdateRequest,
        request: Request,
    ) -> MCPServerResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.update_mcp_server(server_id, payload)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.delete("/mcp/servers/{server_id}", status_code=204)
    async def delete_mcp_server(server_id: str, request: Request) -> None:
        service: OrchestrationService = request.app.state.service
        try:
            await service.delete_mcp_server(server_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.post("/mcp/servers/{server_id}/discover", response_model=MCPServerResponse)
    async def discover_mcp_server(server_id: str, request: Request) -> MCPServerResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.discover_mcp_server(server_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except RuntimeError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/mcp/proxy/{session_id}")
    async def mcp_proxy(session_id: str, request: Request) -> dict[str, object] | None:
        service: OrchestrationService = request.app.state.service
        payload = await request.json()
        if not isinstance(payload, dict):
            raise HTTPException(status_code=400, detail="MCP proxy request must be a JSON object")
        return await service.handle_mcp_proxy_request(
            session_id=session_id,
            authorization=request.headers.get("authorization"),
            payload=payload,
        )

    @app.get("/models/health")
    async def models_health(request: Request, limit: int = 50, offset: int = 0) -> list[dict[str, object]]:
        service: OrchestrationService = request.app.state.service
        return await service.model_health(limit=_page_limit(limit), offset=_page_offset(offset))

    @app.get("/metrics/system", response_model=SystemMetricsResponse)
    async def metrics_system(request: Request) -> SystemMetricsResponse:
        service: OrchestrationService = request.app.state.service
        return await service.system_metrics()

    @app.get("/runtime/status", response_model=RuntimeStatusResponse)
    async def runtime_status(request: Request) -> RuntimeStatusResponse:
        service: OrchestrationService = request.app.state.service
        return await service.runtime_status()

    @app.get("/runtime/sandbox", response_model=SandboxStatusResponse)
    async def runtime_sandbox(request: Request) -> SandboxStatusResponse:
        service: OrchestrationService = request.app.state.service
        return service.sandbox_status()

    return app
