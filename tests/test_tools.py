from __future__ import annotations

import hashlib
import pathlib

from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.schemas import ApprovalStatus, ToolCall
from synode.tools import build_tool_registry
from synode.tools.base import ToolExecutor
from synode.tools.sandbox import SandboxResult, SandboxStatus


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


async def test_file_list_lists_workspace_files(
    database,
    tool_executor: ToolExecutor,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "module.py").write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("list files", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    result = await tool_executor.execute(
        run_id,
        "coder",
        str(tmp_path),
        ToolCall(name="native.fs_list", arguments={"glob": "*.py"}),
    )

    assert result.ok
    assert result.output["matches"] == [{"path": "module.py", "size": len("def add(left, right):\n    return left + right\n")}]


async def test_file_search_rejects_glob_as_pattern(
    database,
    tool_executor: ToolExecutor,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "module.py").write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("search files", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    result = await tool_executor.execute(
        run_id,
        "coder",
        str(tmp_path),
        ToolCall(name="native.fs_search", arguments={"pattern": "*.py", "glob": "*.py"}),
    )

    assert not result.ok
    assert result.error is not None
    assert "pattern is a regex" in result.error
    assert result.output["suggested_call"]["name"] == "native.fs_list"


async def test_file_search_invalid_regex_returns_tool_error(
    database,
    tool_executor: ToolExecutor,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "module.py").write_text("def add(left, right):\n    return left + right\n", encoding="utf-8")
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("search files", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    result = await tool_executor.execute(
        run_id,
        "coder",
        str(tmp_path),
        ToolCall(name="native.fs_search", arguments={"pattern": "(", "glob": "*.py"}),
    )
    async with database.session() as session:
        audits = await Repository(session).list_tool_audit(run_id)

    assert not result.ok
    assert result.error is not None
    assert "invalid regex pattern" in result.error
    assert audits[-1].status == "error"


async def test_file_search_requires_pattern_and_rejects_root(
    database,
    tool_executor: ToolExecutor,
    tmp_path: pathlib.Path,
) -> None:
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("search files", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    no_pattern = await tool_executor.execute(
        run_id,
        "coder",
        str(tmp_path),
        ToolCall(name="native.fs_search", arguments={"glob": "*.py"}),
    )
    with_root = await tool_executor.execute(
        run_id,
        "coder",
        str(tmp_path),
        ToolCall(name="native.fs_search", arguments={"root": str(tmp_path), "pattern": "def", "glob": "*.py"}),
    )

    assert not no_pattern.ok
    assert no_pattern.error is not None
    assert "pattern is required" in no_pattern.error
    assert no_pattern.output["suggested_call"]["name"] == "native.fs_list"
    assert not with_root.ok
    assert with_root.error is not None
    assert "unexpected arguments" in with_root.error
    assert "root" in with_root.error


async def test_mcp_proxy_session_enforces_token_scope_and_audit(
    database,
    tool_executor: ToolExecutor,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("read through proxy", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id
        thread_id = run.thread_id

    proxy = await tool_executor.create_proxy_session(
        run_id=run_id,
        thread_id=thread_id,
        node_id="data_analyst",
        role_name="data_analyst",
        backend_id="openhands",
        workspace=str(tmp_path),
    )
    bad = await tool_executor.handle_mcp_proxy_request(
        session_id=proxy.session_id,
        token="wrong",
        payload={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
    )
    listed = await tool_executor.handle_mcp_proxy_request(
        session_id=proxy.session_id,
        token=proxy.token,
        payload={"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
    )
    denied = await tool_executor.handle_mcp_proxy_request(
        session_id=proxy.session_id,
        token=proxy.token,
        payload={
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "native.fs_write", "arguments": {"path": "x.txt", "content": "no"}},
        },
    )
    read = await tool_executor.handle_mcp_proxy_request(
        session_id=proxy.session_id,
        token=proxy.token,
        payload={
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "native.fs_read", "arguments": {"path": "README.md"}},
        },
    )
    async with database.session() as session:
        audits = await Repository(session).list_tool_audit(run_id)

    assert bad is not None and bad["error"]["message"] == "invalid MCP proxy token"
    assert listed is not None
    listed_tools = {tool["name"] for tool in listed["result"]["tools"]}
    assert "native.fs_read" in listed_tools
    assert "native.fs_list" in listed_tools
    assert "native.fs_write" not in listed_tools
    fs_search_schema = next(tool["inputSchema"] for tool in listed["result"]["tools"] if tool["name"] == "native.fs_search")
    assert fs_search_schema["additionalProperties"] is False
    assert fs_search_schema["required"] == ["pattern"]
    assert denied is not None and "not allowed" in denied["error"]["message"]
    assert read is not None and read["result"]["structuredContent"]["ok"] is True
    assert read["result"]["structuredContent"]["output"]["content"] == "# Demo\n"
    assert [record.tool_name for record in audits] == ["native.fs_read"]


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


async def test_approved_write_mutation_runs_inside_sandbox(
    database, tool_executor: ToolExecutor, tmp_path: pathlib.Path
) -> None:
    tool_executor.sandbox = FailingMutationSandbox()
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("write through sandbox", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    call = ToolCall(name="native.fs_write", arguments={"path": "sandboxed.txt", "content": "ok"})
    first = await tool_executor.execute(run_id, "coder", str(tmp_path), call)
    assert first.approval_id

    async with database.session() as session:
        repo = Repository(session)
        await repo.decide_approval(first.approval_id, ApprovalStatus.APPROVED, "test")

    second = await tool_executor.execute(run_id, "coder", str(tmp_path), call)

    assert not second.ok
    assert second.error == "sandbox denied mutation"
    assert second.output["sandbox_execution"] == "sandbox_runner"
    assert not (tmp_path / "sandboxed.txt").exists()


async def test_patch_apply_mutation_runs_inside_sandbox(
    database, tool_executor: ToolExecutor, tmp_path: pathlib.Path
) -> None:
    target = tmp_path / "module.py"
    original = "value = 'old'\n"
    target.write_text(original, encoding="utf-8")
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("patch through sandbox", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    call = ToolCall(
        name="native.patch_apply",
        arguments={
            "patches": [
                {
                    "path": "module.py",
                    "expected_sha256": digest,
                    "old_text": "'old'",
                    "new_text": "'new'",
                }
            ]
        },
    )
    first = await tool_executor.execute(run_id, "coder", str(tmp_path), call)
    assert first.approval_id

    async with database.session() as session:
        repo = Repository(session)
        await repo.decide_approval(first.approval_id, ApprovalStatus.APPROVED, "test")

    second = await tool_executor.execute(run_id, "coder", str(tmp_path), call)

    assert second.ok
    assert second.output["sandbox_execution"] == "sandbox_runner"
    assert second.output["changed"] == [{"path": str(target), "old_sha256": digest}]
    assert target.read_text(encoding="utf-8") == "value = 'new'\n"


async def test_patch_apply_supports_multiple_patches_for_one_file(
    database, tool_executor: ToolExecutor, tmp_path: pathlib.Path
) -> None:
    target = tmp_path / "module.py"
    original = "alpha = 'old'\nbeta = 'old'\n"
    target.write_text(original, encoding="utf-8")
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("patch one file twice", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    call = ToolCall(
        name="native.patch_apply",
        arguments={
            "patches": [
                {
                    "path": "module.py",
                    "expected_sha256": digest,
                    "old_text": "alpha = 'old'",
                    "new_text": "alpha = 'new'",
                },
                {
                    "path": "module.py",
                    "expected_sha256": digest,
                    "old_text": "beta = 'old'",
                    "new_text": "beta = 'new'",
                },
            ]
        },
    )
    first = await tool_executor.execute(run_id, "coder", str(tmp_path), call)
    assert first.approval_id

    async with database.session() as session:
        repo = Repository(session)
        await repo.decide_approval(first.approval_id, ApprovalStatus.APPROVED, "test")

    second = await tool_executor.execute(run_id, "coder", str(tmp_path), call)

    assert second.ok
    assert second.output["changed"] == [{"path": str(target), "old_sha256": digest}]
    assert target.read_text(encoding="utf-8") == "alpha = 'new'\nbeta = 'new'\n"


async def test_patch_apply_does_not_partially_write_when_later_patch_fails(
    database, tool_executor: ToolExecutor, tmp_path: pathlib.Path
) -> None:
    target = tmp_path / "module.py"
    original = "alpha = 'old'\nbeta = 'old'\n"
    target.write_text(original, encoding="utf-8")
    digest = hashlib.sha256(original.encode("utf-8")).hexdigest()
    async with database.session() as session:
        repo = Repository(session)
        run = await repo.create_run("patch one file atomically", model_provider="fake", workspace=str(tmp_path))
        run_id = run.id

    call = ToolCall(
        name="native.patch_apply",
        arguments={
            "patches": [
                {
                    "path": "module.py",
                    "expected_sha256": digest,
                    "old_text": "alpha = 'old'",
                    "new_text": "alpha = 'new'",
                },
                {
                    "path": "module.py",
                    "expected_sha256": digest,
                    "old_text": "gamma = 'old'",
                    "new_text": "gamma = 'new'",
                },
            ]
        },
    )
    first = await tool_executor.execute(run_id, "coder", str(tmp_path), call)
    assert first.approval_id

    async with database.session() as session:
        repo = Repository(session)
        await repo.decide_approval(first.approval_id, ApprovalStatus.APPROVED, "test")

    second = await tool_executor.execute(run_id, "coder", str(tmp_path), call)

    assert not second.ok
    assert "old_text must occur exactly once" in str(second.error)
    assert target.read_text(encoding="utf-8") == original


class FailingMutationSandbox:
    def ensure_available(self) -> None:
        return None

    def status(self) -> SandboxStatus:
        return SandboxStatus(
            backend="process",
            available=True,
            detail="test sandbox",
            cpu_seconds=1,
            memory_mb=64,
            disk_mb=64,
            output_max_bytes=12000,
        )

    async def run_python(
        self,
        code: str,
        *,
        cwd: pathlib.Path,
        timeout: float,
        env: dict[str, str] | None = None,
    ) -> SandboxResult:
        assert code
        assert cwd.exists()
        assert env
        return SandboxResult(
            ok=False,
            argv=["python", "-I", "-c", code],
            returncode=2,
            stdout='{"ok":false,"error":"sandbox denied mutation"}',
            stderr="",
        )
