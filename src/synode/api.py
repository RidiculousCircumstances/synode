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
    ApprovalDecision,
    ApprovalResponse,
    ApprovalStatus,
    ArtifactResponse,
    RunCreateRequest,
    RunEventResponse,
    RunMetricsResponse,
    RunMode,
    RunResponse,
    RunStatus,
    SystemMetricsResponse,
    ToolAuditResponse,
)


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

    @app.post("/runs", response_model=RunResponse)
    async def create_run(payload: RunCreateRequest, request: Request) -> RunResponse:
        service: OrchestrationService = request.app.state.service
        run = await service.create_run(payload.task, payload.workspace, payload.model_provider, payload.mode)
        asyncio.create_task(service.execute_run(run.id))
        return run

    @app.get("/runs", response_model=list[RunResponse])
    async def list_runs(
        request: Request,
        status: RunStatus | None = None,
        mode: RunMode | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RunResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_runs(status=status, mode=mode, limit=min(limit, 200), offset=offset)

    @app.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str, request: Request) -> RunResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.get_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/events", response_model=list[RunEventResponse])
    async def get_events(run_id: str, request: Request, after_id: int = 0) -> list[RunEventResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_event_responses(run_id, after_id=after_id)

    @app.get("/runs/{run_id}/events/stream")
    async def stream_events(run_id: str, request: Request, after_id: int = 0) -> StreamingResponse:
        service: OrchestrationService = request.app.state.service

        async def generator() -> AsyncIterator[str]:
            header_last_id = request.headers.get("last-event-id")
            cursor = int(header_last_id) if header_last_id and header_last_id.isdigit() else after_id
            while True:
                events = await service.list_event_responses(run_id, after_id=cursor)
                for event in events:
                    cursor = event.id
                    yield (
                        f"id: {event.id}\n"
                        f"event: {event.event_type}\n"
                        f"data: {event.model_dump_json()}\n\n"
                    )
                run = await service.get_run(run_id)
                if run.status.value in {"completed", "failed", "failed_verification", "waiting_approval"}:
                    break
                if not events:
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(generator(), media_type="text/event-stream")

    @app.get("/runs/{run_id}/artifacts", response_model=list[ArtifactResponse])
    async def get_artifacts(run_id: str, request: Request) -> list[ArtifactResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_artifacts(run_id)

    @app.get("/runs/{run_id}/tool-audit", response_model=list[ToolAuditResponse])
    async def get_tool_audit(run_id: str, request: Request) -> list[ToolAuditResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_tool_audit(run_id)

    @app.get("/runs/{run_id}/approvals", response_model=list[ApprovalResponse])
    async def get_run_approvals(run_id: str, request: Request) -> list[ApprovalResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_approvals(run_id=run_id)

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
        asyncio.create_task(service.resume_run(run_id))
        return {"status": "scheduled"}

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

    @app.get("/approvals", response_model=list[ApprovalResponse])
    async def list_approvals(
        request: Request, status: ApprovalStatus | None = None
    ) -> list[ApprovalResponse]:
        service: OrchestrationService = request.app.state.service
        return await service.list_approvals(status=status)

    @app.get("/agents")
    async def agents(request: Request) -> list[dict[str, object]]:
        service: OrchestrationService = request.app.state.service
        return service.roles.as_public()

    @app.get("/tools")
    async def tools(request: Request) -> dict[str, list[str]]:
        service: OrchestrationService = request.app.state.service
        return {"tools": service.tools.list_names()}

    @app.get("/mcp/tools")
    async def mcp_tools(request: Request) -> dict[str, list[str]]:
        service: OrchestrationService = request.app.state.service
        return {"tools": [name for name in service.tools.list_names() if name.startswith("mcp.")]}

    @app.get("/models/health")
    async def models_health(request: Request) -> list[dict[str, object]]:
        service: OrchestrationService = request.app.state.service
        return await service.model_health()

    @app.get("/metrics/system", response_model=SystemMetricsResponse)
    async def metrics_system(request: Request) -> SystemMetricsResponse:
        service: OrchestrationService = request.app.state.service
        return await service.system_metrics()

    return app
