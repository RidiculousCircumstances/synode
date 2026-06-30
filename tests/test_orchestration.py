from __future__ import annotations

import pathlib
from subprocess import run

from sqlalchemy import select

from synode.persistence.models import ApprovalRecord
from synode.schemas import ApprovalStatus, RunMode, RunStatus


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
