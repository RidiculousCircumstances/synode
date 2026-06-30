from __future__ import annotations

import pathlib

from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.schemas import ApprovalStatus, ToolCall
from synode.tools import build_tool_registry
from synode.tools.base import ToolExecutor


async def test_data_profile_tool_profiles_csv(service, tmp_path: pathlib.Path) -> None:
    path = tmp_path / "data.csv"
    path.write_text("name,value\nalpha,10\nbeta,20\n", encoding="utf-8")
    run = await service.create_run("Analyze data", workspace=str(tmp_path), model_provider="fake")
    result = await service.tool_executor.execute(
        run.id,
        "data_analyst",
        str(tmp_path),
        ToolCall(name="native.data_profile", arguments={"path": "data.csv"}),
    )
    assert result.ok
    assert result.output["rows"] == 2
    assert result.output["numeric_summary"]["value"]["mean"] == 15


async def test_write_tool_requires_approval_and_resumes(
    database, tool_executor: ToolExecutor, tmp_path: pathlib.Path
) -> None:
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("write a file", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    call = ToolCall(name="native.fs_write", arguments={"path": "result.txt", "content": "ok"})
    first = await tool_executor.execute(run_id, "coder", str(tmp_path), call)
    assert not first.ok
    assert first.approval_id

    async with database.session() as session:
        repo = Repository(session)
        await repo.decide_approval(first.approval_id, ApprovalStatus.APPROVED, "test")

    second = await tool_executor.execute(run_id, "coder", str(tmp_path), call)
    assert second.ok
    assert (tmp_path / "result.txt").read_text(encoding="utf-8") == "ok"


async def test_approved_write_fails_closed_without_sandbox_backend(
    settings, database, tmp_path: pathlib.Path
) -> None:
    sandboxless_settings = settings.model_copy(update={"sandbox_backend": "none"})
    tools = await build_tool_registry(sandboxless_settings, include_mcp=False)
    executor = ToolExecutor(
        database,
        RoleRegistry.load_builtin(),
        tools,
        sandboxless_settings,
    )
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("write a file", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    call = ToolCall(name="native.fs_write", arguments={"path": "blocked.txt", "content": "nope"})
    first = await executor.execute(run_id, "coder", str(tmp_path), call)
    assert first.approval_id

    async with database.session() as session:
        repo = Repository(session)
        await repo.decide_approval(first.approval_id, ApprovalStatus.APPROVED, "test")

    second = await executor.execute(run_id, "coder", str(tmp_path), call)

    assert not second.ok
    assert second.error is not None
    assert "sandbox unavailable" in second.error
    assert not (tmp_path / "blocked.txt").exists()
