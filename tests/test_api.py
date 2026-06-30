from __future__ import annotations

import pathlib

import httpx

from synode.api import create_app
from synode.runtime.service import OrchestrationService
from synode.schemas import RunStatus


async def test_api_exposes_run_read_models(service: OrchestrationService, tmp_path: pathlib.Path) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n", encoding="utf-8")
    run = await service.run_task("Analyze sample data", workspace=str(tmp_path), model_provider="fake")

    app = create_app()
    app.state.service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        run_response = (await client.get(f"/runs/{run.id}")).json()
        runs_response = (await client.get("/runs")).json()
        events_response = (await client.get(f"/runs/{run.id}/events")).json()
        artifacts_response = (await client.get(f"/runs/{run.id}/artifacts")).json()
        audit_response = (await client.get(f"/runs/{run.id}/tool-audit")).json()
        metrics_response = (await client.get(f"/runs/{run.id}/metrics")).json()

    assert run_response["status"] == RunStatus.COMPLETED.value
    assert any(item["id"] == run.id for item in runs_response)
    assert any(event["event_type"] == "node_started" for event in events_response)
    assert any(artifact["kind"] == "final_answer" for artifact in artifacts_response)
    assert any(record["tool_name"] == "native.data_profile" for record in audit_response)
    assert metrics_response["model_call_count"] >= 1
    assert metrics_response["tool_call_count"] >= 1


async def test_api_streams_sse_events(service: OrchestrationService, tmp_path: pathlib.Path) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n", encoding="utf-8")
    run = await service.run_task("Analyze sample data", workspace=str(tmp_path), model_provider="fake")

    app = create_app()
    app.state.service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        async with client.stream("GET", f"/runs/{run.id}/events/stream") as response:
            body = await response.aread()

    text = body.decode("utf-8")
    assert "event: run_created" in text
    assert "event: run_completed" in text
    assert "data: " in text


async def test_api_allows_private_lan_ui_origin() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        response = await client.get("/health", headers={"Origin": "http://192.168.1.50:3000"})

    assert response.headers["access-control-allow-origin"] == "http://192.168.1.50:3000"
