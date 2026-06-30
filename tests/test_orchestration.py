from __future__ import annotations

import pathlib
from subprocess import run

import pytest
from sqlalchemy import select

from synode.persistence.models import ApprovalRecord
from synode.persistence.repository import Repository
from synode.schemas import ApprovalStatus, EventType, RunMode, RunStatus


async def test_run_task_completes_with_fake_data_agent(service, tmp_path: pathlib.Path) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n2026-06-02,20\n", encoding="utf-8")
    result = await service.run_task(
        "Analyze sample data and summarize findings",
        workspace=str(tmp_path),
        model_provider="fake",
    )
    assert result.status == RunStatus.COMPLETED
    assert result.final_answer is not None
    assert "data_analyst" in result.final_answer
    assert "numeric_summary" in result.final_answer


async def test_coding_workflow_requires_approval_then_applies_patch(service, database, tmp_path: pathlib.Path) -> None:
    run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")

    first = await service.run_task(
        "Make a tiny README wording change",
        workspace=str(tmp_path),
        model_provider="fake",
        mode=RunMode.CODING,
    )
    assert first.status == RunStatus.WAITING_APPROVAL
    async with database.session() as session:
        approval = (
            await session.execute(
                select(ApprovalRecord).where(
                    ApprovalRecord.run_id == first.id,
                    ApprovalRecord.status == ApprovalStatus.PENDING.value,
                )
            )
        ).scalars().one()

    await service.approve(approval.id, "test approval")
    await service.resume_run(first.id)
    second = await service.get_run(first.id)

    assert second.status == RunStatus.COMPLETED
    assert "Synode coding workflow smoke." in (tmp_path / "README.md").read_text(encoding="utf-8")
    assert second.final_answer is not None
    assert "[verification]" in second.final_answer


async def test_coding_workflow_failed_tests_set_failed_verification(
    service, database, tmp_path: pathlib.Path
) -> None:
    run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "test_smoke.py").write_text("def test_smoke():\n    assert False\n", encoding="utf-8")

    first = await service.run_task(
        "Make a tiny README wording change",
        workspace=str(tmp_path),
        model_provider="fake",
        mode=RunMode.CODING,
    )
    async with database.session() as session:
        approval = (
            await session.execute(
                select(ApprovalRecord).where(
                    ApprovalRecord.run_id == first.id,
                    ApprovalRecord.status == ApprovalStatus.PENDING.value,
                )
            )
        ).scalars().one()

    await service.approve(approval.id, "test approval")
    await service.resume_run(first.id)
    second = await service.get_run(first.id)

    assert second.status == RunStatus.FAILED_VERIFICATION
    assert second.final_answer is not None
    assert "verification failed" in second.final_answer


async def test_rejecting_approval_cancels_run_and_unblocks_thread(
    service, database, tmp_path: pathlib.Path
) -> None:
    run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    (tmp_path / "test_smoke.py").write_text("def test_smoke():\n    assert True\n", encoding="utf-8")

    first = await service.run_task(
        "Make a tiny README wording change",
        workspace=str(tmp_path),
        model_provider="fake",
        mode=RunMode.CODING,
    )
    assert first.status == RunStatus.WAITING_APPROVAL
    async with database.session() as session:
        approval = (
            await session.execute(
                select(ApprovalRecord).where(
                    ApprovalRecord.run_id == first.id,
                    ApprovalRecord.status == ApprovalStatus.PENDING.value,
                )
            )
        ).scalars().one()

    await service.reject(approval.id, "not this patch")
    rejected = await service.get_run(first.id)
    follow_up = await service.create_thread_run(
        rejected.thread_id,
        "Continue with a different approach",
        workspace=str(tmp_path),
        model_provider="fake",
    )

    assert rejected.status == RunStatus.CANCELLED
    assert rejected.final_answer == "Approval rejected for native.patch_apply."
    assert follow_up.status == RunStatus.CREATED
    with pytest.raises(ValueError, match="run is terminal"):
        await service.resume_run(first.id)


async def test_stop_created_run_cancels_and_unblocks_thread(service, tmp_path: pathlib.Path) -> None:
    run_response = await service.create_run(
        "Inspect the workspace",
        workspace=str(tmp_path),
        model_provider="fake",
    )

    stopped = await service.stop_run(run_response.id, "No longer needed")
    follow_up = await service.create_thread_run(
        stopped.thread_id,
        "Continue with a new request",
        workspace=str(tmp_path),
        model_provider="fake",
    )

    assert stopped.status == RunStatus.CANCELLED
    assert stopped.final_answer == "No longer needed"
    assert follow_up.status == RunStatus.CREATED


async def test_run_metrics_are_not_limited_by_event_page_size(service, database, tmp_path: pathlib.Path) -> None:
    run_response = await service.create_run(
        "Collect metric events",
        workspace=str(tmp_path),
        model_provider="fake",
    )
    async with database.session() as session:
        repo = Repository(session)
        for _ in range(205):
            await repo.add_event(
                run_response.id,
                EventType.MODEL_INVOKED.value,
                "tester",
                {
                    "provider": "fake",
                    "role": "tester",
                    "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                    "latency_ms": 1,
                },
            )

    metrics = await service.run_metrics(run_response.id)

    assert metrics.event_count == 206
    assert metrics.model_call_count == 205
    assert metrics.token_usage.total_tokens == 615
    assert metrics.latency_ms_by_role["tester"] == 205
