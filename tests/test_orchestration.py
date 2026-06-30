from __future__ import annotations

import pathlib

from synode.schemas import RunStatus


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

