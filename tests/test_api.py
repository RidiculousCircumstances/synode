from __future__ import annotations

import pathlib

import httpx
import pytest

from synode.api import create_app
from synode.persistence.repository import Repository
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
    assert run_response["thread_id"]
    assert any(item["id"] == run.id for item in runs_response)
    assert any(event["event_type"] == "node_started" for event in events_response)
    assert any(artifact["kind"] == "final_answer" for artifact in artifacts_response)
    assert any(record["tool_name"] == "native.data_profile" for record in audit_response)
    assert metrics_response["model_call_count"] >= 1
    assert metrics_response["tool_call_count"] >= 1


async def test_api_exposes_thread_workflow(
    service: OrchestrationService, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def discard_background_run(run_id: str) -> None:
        assert run_id

    monkeypatch.setattr(service, "start_run", discard_background_run)
    app = create_app()
    app.state.service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        created = (
            await client.post(
                "/threads",
                json={
                    "title": "Investigate a flaky test",
                    "message": "Inspect the test suite and propose a fix",
                    "model_provider": "fake",
                    "mode": "coding",
                },
            )
        ).json()
        thread_id = created["thread"]["id"]
        first_run_id = created["thread"]["latest_run_id"]

        listed = (await client.get("/threads")).json()
        busy = await client.post(
            f"/threads/{thread_id}/runs",
            json={"message": "This should wait for the active run", "model_provider": "fake"},
        )

        async with service.database.session() as session:
            repo = Repository(session)
            await repo.set_run_status(first_run_id, RunStatus.COMPLETED)

        follow_up = (
            await client.post(
                f"/threads/{thread_id}/runs",
                json={"message": "Now summarize the next patch", "model_provider": "fake"},
            )
        ).json()
        detail = (await client.get(f"/threads/{thread_id}")).json()
        messages = (await client.get(f"/threads/{thread_id}/messages")).json()
        paged_detail = (
            await client.get(
                f"/threads/{thread_id}",
                params={"runs_limit": 1, "messages_limit": 1, "messages_offset": 1},
            )
        ).json()
        paged_messages = (
            await client.get(f"/threads/{thread_id}/messages", params={"limit": 1, "offset": 1})
        ).json()
        archived = (await client.post(f"/threads/{thread_id}/archive")).json()
        blocked = await client.post(
            f"/threads/{thread_id}/runs",
            json={"message": "This should not start", "model_provider": "fake"},
        )

    assert created["thread"]["title"] == "Investigate a flaky test"
    assert created["runs"][0]["thread_id"] == thread_id
    assert first_run_id == created["runs"][0]["id"]
    assert any(item["id"] == thread_id for item in listed)
    assert busy.status_code == 409
    assert follow_up["thread_id"] == thread_id
    assert {run["id"] for run in detail["runs"]} == {first_run_id, follow_up["id"]}
    assert [message["author_type"] for message in messages].count("user") == 2
    assert len(paged_detail["runs"]) == 1
    assert len(paged_detail["messages"]) == 1
    assert paged_detail["messages"][0]["content"] == messages[1]["content"]
    assert len(paged_messages) == 1
    assert paged_messages[0]["content"] == messages[1]["content"]
    assert archived["status"] == "archived"
    assert blocked.status_code == 409


async def test_api_stops_run_without_request_body(service: OrchestrationService, tmp_path: pathlib.Path) -> None:
    run = await service.create_run("Inspect this later", workspace=str(tmp_path), model_provider="fake")

    app = create_app()
    app.state.service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        stopped = (await client.post(f"/runs/{run.id}/stop")).json()
        events = (await client.get(f"/runs/{run.id}/events")).json()

    assert stopped["status"] == RunStatus.CANCELLED.value
    assert stopped["final_answer"] == "Run stopped by user."
    assert any(
        event["event_type"] == "run_cancelled" and event["payload"]["reason"] == "Run stopped by user."
        for event in events
    )


async def test_api_queues_runs_and_exposes_runtime_status(
    service: OrchestrationService, tmp_path: pathlib.Path
) -> None:
    app = create_app()
    app.state.service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        created = (
            await client.post(
                "/runs",
                json={"task": "Queue this run", "workspace": str(tmp_path), "model_provider": "fake"},
            )
        ).json()
        runtime = (await client.get("/runtime/status")).json()
        sandbox = (await client.get("/runtime/sandbox")).json()

    assert created["status"] == RunStatus.QUEUED.value
    assert runtime["queue_depth"] == 1
    assert runtime["queue"]["backend"] == "in_memory"
    assert runtime["queue"]["available"] is True
    assert runtime["execution_backends"]["native_langgraph"]["available"] is True
    assert runtime["execution_backends"]["openhands"]["available"] is False
    assert runtime["worker_concurrency"] == service.settings.worker_concurrency
    assert runtime["secrets_configured"] is False
    assert runtime["sandbox"]["available"] is True
    assert sandbox["backend"] == "process"


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


async def test_api_tests_model_profile_capabilities(service: OrchestrationService) -> None:
    app = create_app()
    app.state.service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        profile = (
            await client.post(
                "/model-profiles",
                json={"name": "fake profile test", "provider_type": "fake", "model": "fake"},
            )
        ).json()
        result = (await client.post(f"/model-profiles/{profile['id']}/test")).json()

    assert result["ok"] is True
    assert result["capabilities"]["structured_output"] is True
    assert result["capabilities"]["streaming"] is False
    assert {check["name"] for check in result["checks"]} == {"health", "structured_output", "streaming"}
    assert next(check for check in result["checks"] if check["name"] == "streaming")["supported"] is False


async def test_api_tests_disabled_model_profile_without_provider_calls(service: OrchestrationService) -> None:
    app = create_app()
    app.state.service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        profile = (
            await client.post(
                "/model-profiles",
                json={"name": "disabled fake profile", "provider_type": "fake", "model": "fake", "enabled": False},
            )
        ).json()
        result = (await client.post(f"/model-profiles/{profile['id']}/test")).json()

    assert result["ok"] is False
    assert result["checks"] == [
        {
            "name": "health",
            "ok": False,
            "supported": True,
            "latency_ms": None,
            "error": "profile is disabled",
        }
    ]


async def test_api_test_model_profile_missing_returns_404(service: OrchestrationService) -> None:
    app = create_app()
    app.state.service = service
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        response = await client.post("/model-profiles/missing/test")

    assert response.status_code == 404


async def test_api_allows_private_lan_ui_origin() -> None:
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://synode.test") as client:
        response = await client.get("/health", headers={"Origin": "http://192.168.1.50:3000"})

    assert response.headers["access-control-allow-origin"] == "http://192.168.1.50:3000"
