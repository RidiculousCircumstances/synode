from __future__ import annotations

import pathlib

from synode.evals.coding import changed_files, load_tasks, materialize_task, run_workspace_tests


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
