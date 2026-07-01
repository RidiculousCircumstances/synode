from __future__ import annotations

import pathlib
from typing import Any

from synode.evals.coding import (
    behavior_pass,
    changed_files,
    collect_behavior_metrics,
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
    assert "sql_refund_revenue_query" in tasks
    assert "sh_retention_filter" in tasks
    assert "tool_argument_repair_probe" in tasks
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


def test_behavior_metrics_detect_loop_protocol_failures() -> None:
    task = next(task for task in load_tasks() if task.id == "tool_argument_repair_probe")
    duplicate_call = {"name": "native.fs_list", "arguments": {"glob": "*.py"}}
    client = FakeBehaviorClient(
        artifacts=[
            {
                "kind": "native_loop_trace",
                "content": {
                    "steps": [
                        {
                            "step": 1,
                            "action": "finish",
                            "summary": "premature",
                            "payload": {},
                            "error": "final payload does not match contract coding_inspection",
                        },
                        {
                            "step": 2,
                            "action": "tool_call",
                            "summary": "list files",
                            "tool_call": duplicate_call,
                            "observation": "{}",
                        },
                        {
                            "step": 3,
                            "action": "tool_call",
                            "summary": "list files again",
                            "tool_call": duplicate_call,
                            "error": "duplicate tool call repeated without new information",
                        },
                    ]
                },
            },
            {"kind": "patch_proposal", "content": {"action": "patch"}},
            {"kind": "run_report", "content": {}},
        ],
        audit=[
            {
                "tool_name": "native.fs_search",
                "status": "error",
                "input": {"glob": "*.py"},
                "output": {"error": "pattern is required for native.fs_search; use native.fs_list to list files"},
            },
            {
                "tool_name": "native.fs_search",
                "status": "ok",
                "input": {"pattern": "calculate_platform_fee", "glob": "*.py"},
                "output": {},
            },
            {"tool_name": "native.patch_apply", "status": "ok", "input": {}, "output": {}},
            {"tool_name": "native.verify", "status": "ok", "input": {}, "output": {}},
        ],
    )

    metrics = collect_behavior_metrics(
        client,
        "run-1",
        task=task,
        backend="native_langgraph",
        status="completed",
        changed_file_count=1,
        hidden_tests_pass=True,
    )

    assert metrics.first_action_kind == "finish"
    assert metrics.first_action_tool_call_pass is False
    assert metrics.schema_valid_json_pass is False
    assert metrics.schema_recovered_after_validation_error is True
    assert metrics.duplicate_tool_call_pass is False
    assert metrics.invalid_arg_repair_applicable is True
    assert metrics.invalid_arg_repair_pass is True
    assert metrics.patch_verify_pass is True
    assert metrics.grounded_success_pass is True


def test_behavior_metrics_reject_ungrounded_completed_mutation() -> None:
    task = next(task for task in load_tasks() if task.id == "tool_argument_repair_probe")
    client = FakeBehaviorClient(
        artifacts=[
            {
                "kind": "native_loop_trace",
                "content": {
                    "steps": [
                        {
                            "step": 1,
                            "action": "tool_call",
                            "tool_call": {"name": "native.fs_list", "arguments": {"glob": "*.py"}},
                            "observation": "{}",
                        }
                    ]
                },
            },
            {"kind": "final_answer", "content": {"text": "Done"}},
        ],
        audit=[],
    )

    metrics = collect_behavior_metrics(
        client,
        "run-1",
        task=task,
        backend="native_langgraph",
        status="completed",
        changed_file_count=0,
        hidden_tests_pass=False,
    )

    assert metrics.first_action_tool_call_pass is True
    assert metrics.grounded_success_pass is False
    assert metrics.ungrounded_success is True


def test_behavior_metrics_count_schema_failures_from_model_events() -> None:
    task = next(task for task in load_tasks() if task.id == "tool_argument_repair_probe")
    client = FakeBehaviorClient(
        events=[
            {
                "id": 1,
                "event_type": "model_invoked",
                "payload": {"ok": False, "error": "model returned invalid JSON: Expecting value"},
            },
            {"id": 2, "event_type": "model_invoked", "payload": {"ok": True}},
        ],
        artifacts=[
            {
                "kind": "native_loop_trace",
                "content": {
                    "steps": [
                        {
                            "step": 1,
                            "action": "tool_call",
                            "tool_call": {"name": "native.fs_list", "arguments": {"glob": "*.py"}},
                            "observation": "{}",
                        }
                    ]
                },
            }
        ],
        audit=[{"tool_name": "native.fs_list", "status": "ok", "input": {}, "output": {}}],
    )

    metrics = collect_behavior_metrics(
        client,
        "run-1",
        task=task,
        backend="native_langgraph",
        status="failed",
        changed_file_count=0,
        hidden_tests_pass=False,
    )

    assert metrics.schema_valid_json_pass is False
    assert metrics.schema_validation_failures == 1
    assert metrics.schema_recovered_after_validation_error is True


def test_behavior_pass_includes_grounded_success() -> None:
    task = next(task for task in load_tasks() if task.id == "tool_argument_repair_probe")
    result = run_task_eval_result_for_behavior()
    result.behavior.grounded_success_pass = False

    assert behavior_pass(task, result) is False


def run_task_eval_result_for_behavior():
    from synode.evals.coding import CodingEvalResult

    result = CodingEvalResult(task_id="tool_argument_repair_probe", title="probe", workspace="/tmp/probe")
    result.behavior.first_action_tool_call_pass = True
    result.behavior.patch_verify_pass = True
    return result


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


class FakeBehaviorClient:
    def __init__(
        self,
        *,
        events: list[dict[str, Any]] | None = None,
        audit: list[dict[str, Any]] | None = None,
        artifacts: list[dict[str, Any]] | None = None,
    ) -> None:
        self.events = events or []
        self.audit = audit or []
        self.artifacts = artifacts or []

    def get(self, path: str) -> Any:
        if path.startswith("/runs/run-1/events"):
            return self.events
        if path.startswith("/runs/run-1/tool-audit"):
            return self.audit
        if path.startswith("/runs/run-1/artifacts"):
            return self.artifacts
        raise AssertionError(f"unexpected GET {path}")
