from __future__ import annotations

import asyncio
import pathlib
from datetime import UTC, datetime, timedelta

from synode.persistence.repository import Repository
from synode.runtime.worker import RunWorker
from synode.schemas import EventType, RunStatus, ToolRisk


async def test_worker_claims_queued_run_and_completes(service, tmp_path: pathlib.Path) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n", encoding="utf-8")
    run = await service.create_run("Analyze sample data", workspace=str(tmp_path), model_provider="fake")

    await service.start_run(run.id)
    queued = await service.get_run(run.id)

    worker = RunWorker(service, worker_id="test-worker")
    did_work = await worker.run_once()
    completed = await service.get_run(run.id)
    runtime = await service.runtime_status()

    assert queued.status == RunStatus.QUEUED
    assert did_work is True
    assert completed.status == RunStatus.COMPLETED
    assert completed.worker_id is None
    assert runtime.queue_depth == 0
    assert any(heartbeat.worker_id == "test-worker" for heartbeat in runtime.workers)


async def test_worker_service_honors_configured_concurrency(
    service, database, tmp_path: pathlib.Path, monkeypatch
) -> None:
    service.settings.worker_concurrency = 2
    service.settings.worker_poll_interval_seconds = 0.01
    service.settings.worker_heartbeat_interval_seconds = 0.01
    run_a = await service.create_run("Parallel run A", workspace=str(tmp_path), model_provider="fake")
    run_b = await service.create_run("Parallel run B", workspace=str(tmp_path), model_provider="fake")
    await service.start_run(run_a.id)
    await service.start_run(run_b.id)
    started: list[str] = []
    both_started = asyncio.Event()
    release = asyncio.Event()

    async def execute_run(run_id: str) -> None:
        started.append(run_id)
        if len(started) == 2:
            both_started.set()
        await release.wait()
        async with database.session() as session:
            repo = Repository(session)
            await repo.set_run_status(run_id, RunStatus.COMPLETED, final_answer="done")

    monkeypatch.setattr(service, "execute_run", execute_run)
    worker = RunWorker(service, worker_id="parallel-worker")
    task = asyncio.create_task(worker.serve_forever())
    try:
        await asyncio.wait_for(both_started.wait(), timeout=1)
        running_a = await service.get_run(run_a.id)
        running_b = await service.get_run(run_b.id)

        assert {running_a.status, running_b.status} == {RunStatus.RUNNING}
        assert {running_a.worker_id, running_b.worker_id} == {
            "parallel-worker:slot-1",
            "parallel-worker:slot-2",
        }
    finally:
        release.set()
        worker.stop()
        await asyncio.wait_for(task, timeout=1)

    runtime = await service.runtime_status()

    assert runtime.worker_concurrency == 2
    assert {heartbeat.worker_id for heartbeat in runtime.workers} >= {
        "parallel-worker:slot-1",
        "parallel-worker:slot-2",
    }


async def test_stale_running_run_is_requeued(service, database, tmp_path: pathlib.Path) -> None:
    run = await service.create_run("Recover this", workspace=str(tmp_path), model_provider="fake")
    await service.start_run(run.id)
    claimed = await service.claim_queued_run(run.id, "stale-worker")
    assert claimed is not None

    async with database.session() as session:
        repo = Repository(session)
        record = await repo.get_run(run.id)
        assert record is not None
        record.heartbeat_at = datetime.now(UTC) - timedelta(seconds=1000)
        record.updated_at = datetime.now(UTC) - timedelta(seconds=1000)

    recovered = await service.recover_stale_runs()
    current = await service.get_run(run.id)

    assert recovered["requeued"] == 1
    assert current.status == RunStatus.QUEUED
    assert current.worker_id is None
    assert current.error == "Recovered stale running run after worker heartbeat expired."


async def test_stop_external_running_run_requests_cancellation(service, tmp_path: pathlib.Path) -> None:
    run = await service.create_run("Cancel this", workspace=str(tmp_path), model_provider="fake")
    await service.start_run(run.id)
    claimed = await service.claim_queued_run(run.id, "external-worker")
    assert claimed is not None

    stopped = await service.stop_run(run.id, "operator stop")

    assert stopped.status == RunStatus.CANCELLING
    assert stopped.error == "operator stop"


async def test_retention_cleanup_prunes_old_operational_records(
    service, database, tmp_path: pathlib.Path
) -> None:
    run = await service.create_run("Cleanup old records", workspace=str(tmp_path), model_provider="fake")
    old = datetime.now(UTC) - timedelta(days=60)
    async with database.session() as session:
        repo = Repository(session)
        event = await repo.add_event(run.id, EventType.MODEL_INVOKED.value, "tester", {"ok": True})
        delta = await repo.add_event(run.id, EventType.MODEL_TOKEN_DELTA.value, "tester", {"delta": "x"})
        audit = await repo.add_tool_audit(
            run.id,
            "tester",
            "native.fs_read",
            ToolRisk.READ,
            "ok",
            {},
            {"ok": True},
        )
        artifact = await repo.add_artifact(run.id, "old", {"value": "x"})
        event.created_at = old
        delta.created_at = old
        audit.created_at = old
        artifact.created_at = old

    result = await service.cleanup_retention()

    assert result["run_events_deleted"] >= 1
    assert result["model_deltas_deleted"] >= 1
    assert result["tool_audit_deleted"] >= 1
    assert result["artifacts_deleted"] >= 1
