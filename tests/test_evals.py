from __future__ import annotations

import pathlib
from typing import Any

from synode.evals.coding import (
    changed_files,
    ensure_graph,
    load_tasks,
    map_workspace_for_api,
    materialize_task,
    run_task_eval,
    run_workspace_tests,
)


def test_coding_eval_tasks_load_with_required_benchmarks() -> None:
    tasks = {task.id: task for task in load_tasks()}

    assert "py_ledger_refunds_single_file" in tasks
    assert "py_no_change_guard" in tasks
    assert "py_ambiguous_requirement_operator" in tasks
    assert "contract_unsafe_verification" in tasks
    assert tasks["py_ambiguous_requirement_operator"].expected_operator is True


def test_materialized_eval_task_starts_from_clean_git_baseline(tmp_path: pathlib.Path) -> None:
    task = next(task for task in load_tasks() if task.id == "py_no_change_guard")
    workspace = materialize_task(task, tmp_path)

    assert (workspace / "calculator.py").exists()
    assert changed_files(workspace) == []
    assert run_workspace_tests(workspace, task).returncode == 0


def test_ensure_graph_binds_coder_backend_for_native_and_openhands() -> None:
    native_client = FakeEvalClient()
    openhands_client = FakeEvalClient()

    native = ensure_graph(native_client, "profile-1", backend="native_langgraph")
    openhands = ensure_graph(openhands_client, "profile-1", backend="openhands")

    assert native["node_runtime_bindings"]["coder"] == "native_langgraph"
    assert openhands["node_runtime_bindings"]["coder"] == "openhands"
    assert openhands["node_runtime_bindings"]["supervisor"] == "native_langgraph"
    assert openhands["node_runtime_bindings"]["reviewer"] == "native_langgraph"


def test_openhands_eval_skips_native_contract_only_task(tmp_path: pathlib.Path) -> None:
    task = next(task for task in load_tasks() if task.id == "contract_unsafe_verification")

    result = run_task_eval(
        client=FakeEvalClient(),
        task=task,
        output_root=tmp_path,
        workspace_root=tmp_path,
        api_workspace_root="/workspace/evals/run-1",
        profile_id="profile-1",
        graph_id="graph-1",
        backend="openhands",
        timeout_seconds=1,
        approve_mutations=True,
        skip_contract_only_for_openhands=True,
    )

    assert result.status == "skipped"
    assert result.skipped is True
    assert result.skip_reason is not None
    assert "native PatchProposal" in result.skip_reason


def test_eval_workspace_maps_host_path_to_api_mount(tmp_path: pathlib.Path) -> None:
    root = tmp_path / "host" / "evals" / "run-1"
    workspace = root / "workspaces" / "task-a"

    assert map_workspace_for_api(workspace, root, "/workspace/evals/run-1") == "/workspace/evals/run-1/workspaces/task-a"


class FakeEvalClient:
    def __init__(self) -> None:
        self.graph_payloads: list[dict[str, Any]] = []

    def get(self, path: str) -> Any:
        if path == "/agent-graphs":
            return []
        if path == "/agents":
            return [
                {"id": "role-supervisor", "name": "supervisor", "enabled": True},
                {"id": "role-coder", "name": "coder", "enabled": True},
                {"id": "role-reviewer", "name": "reviewer", "enabled": True},
            ]
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        if path == "/agent-graphs" and payload is not None:
            self.graph_payloads.append(payload)
            return {"id": "graph-1", **payload}
        raise AssertionError(f"unexpected POST {path}")

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        raise AssertionError(f"unexpected PATCH {path}")
