from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from typing import AsyncIterator

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from synode.config import Settings
from synode.runtime.service import OrchestrationService, create_service
from synode.schemas import ApprovalDecision, RunCreateRequest, RunResponse


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    service = await create_service(settings)
    app.state.service = service
    try:
        yield
    finally:
        await service.database.close()


def create_app() -> FastAPI:
    app = FastAPI(title="Synode", version="0.1.0", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/runs", response_model=RunResponse)
    async def create_run(payload: RunCreateRequest, request: Request) -> RunResponse:
        service: OrchestrationService = request.app.state.service
        run = await service.create_run(payload.task, payload.workspace, payload.model_provider)
        asyncio.create_task(service.execute_run(run.id))
        return run

    @app.get("/runs/{run_id}", response_model=RunResponse)
    async def get_run(run_id: str, request: Request) -> RunResponse:
        service: OrchestrationService = request.app.state.service
        try:
            return await service.get_run(run_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/runs/{run_id}/events")
    async def get_events(run_id: str, request: Request, after_id: int = 0) -> list[dict[str, object]]:
        service: OrchestrationService = request.app.state.service
        return await service.list_events(run_id, after_id=after_id)

    @app.get("/runs/{run_id}/events/stream")
    async def stream_events(run_id: str, request: Request) -> StreamingResponse:
        service: OrchestrationService = request.app.state.service

        async def generator() -> AsyncIterator[str]:
            after_id = 0
            while True:
                events = await service.list_events(run_id, after_id=after_id)
                for event in events:
                    after_id = int(event["id"])
                    yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                run = await service.get_run(run_id)
                if run.status.value in {"completed", "failed"}:
                    break
                await asyncio.sleep(0.5)

        return StreamingResponse(generator(), media_type="text/event-stream")

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

    return app

