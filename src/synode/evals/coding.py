from __future__ import annotations

import json
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

TERMINAL_STATUSES = {"completed", "failed", "failed_verification", "cancelled"}
EvalBackend = Literal["native_langgraph", "openhands"]


class EvalApiError(RuntimeError):
    def __init__(self, method: str, path: str, status: int, body: str) -> None:
        super().__init__(f"{method} {path} failed with HTTP {status}: {body}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


@dataclass(frozen=True)
class CodingEvalTask:
    id: str
    title: str
    prompt: str
    public_files: dict[str, str]
    hidden_files: dict[str, str]
    expected_mutation: bool
    expected_operator: bool = False
    contract_only: bool = False


@dataclass
class BehaviorMetrics:
    first_action_kind: str | None = None
    first_action_tool_name: str | None = None
    first_action_tool_call_pass: bool | None = None
    schema_valid_json_pass: bool = True
    schema_validation_failures: int = 0
    schema_recovered_after_validation_error: bool | None = None
    duplicate_tool_call_pass: bool = True
    duplicate_tool_calls: list[str] = field(default_factory=list)
    invalid_arg_errors: int = 0
    invalid_arg_repair_applicable: bool = False
    invalid_arg_repair_pass: bool | None = None
    patch_seen: bool = False
    verify_seen: bool = False
    patch_verify_applicable: bool = False
    patch_verify_pass: bool | None = None
    grounded_success_pass: bool = True
    ungrounded_success: bool = False
    evidence_summary: dict[str, Any] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class CodingEvalResult:
    task_id: str
    title: str
    workspace: str
    api_workspace: str | None = None
    backend: str = "native_langgraph"
    run_id: str | None = None
    thread_id: str | None = None
    status: str | None = None
    skipped: bool = False
    skip_reason: str | None = None
    runtime_pass: bool = False
    functional_pass: bool = False
    safety_pass: bool = False
    contract_pass: bool = False
    behavior_pass: bool = False
    ok: bool = False
    approvals_seen: int = 0
    operator_requests_seen: int = 0
    changed_files: list[str] = field(default_factory=list)
    failure_category: str | None = None
    verification_stdout: str = ""
    verification_stderr: str = ""
    behavior: BehaviorMetrics = field(default_factory=BehaviorMetrics)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class SynodeApiClient:
    base_url: str

    def request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 180,
    ) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        url = urllib.parse.urljoin(self.base_url.rstrip("/") + "/", path.lstrip("/"))
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise EvalApiError(method, path, exc.code, body) from exc
        if not raw:
            return None
        return json.loads(raw)

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, payload: dict[str, Any] | None = None) -> Any:
        return self.request("POST", path, payload or {})

    def patch(self, path: str, payload: dict[str, Any]) -> Any:
        return self.request("PATCH", path, payload)


def load_tasks() -> list[CodingEvalTask]:
    with resources.files("synode.evals").joinpath("coding_tasks.json").open(encoding="utf-8") as handle:
        raw_tasks = json.load(handle)
    return [
        CodingEvalTask(
            id=str(item["id"]),
            title=str(item["title"]),
            prompt=str(item["prompt"]),
            public_files={str(path): str(content) for path, content in item["public_files"].items()},
            hidden_files={str(path): str(content) for path, content in item.get("hidden_files", {}).items()},
            expected_mutation=bool(item.get("expected_mutation", True)),
            expected_operator=bool(item.get("expected_operator", False)),
            contract_only=bool(item.get("contract_only", False)),
        )
        for item in raw_tasks
    ]


def materialize_task(task: CodingEvalTask, root: Path) -> Path:
    workspace = root / "workspaces" / task.id
    if workspace.exists():
        shutil.rmtree(workspace)
    workspace.mkdir(parents=True)
    _write_files(workspace, task.public_files)
    subprocess.run(["git", "init"], cwd=workspace, check=True, capture_output=True)
    subprocess.run(["git", "add", "."], cwd=workspace, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-m", "baseline"],
        cwd=workspace,
        check=True,
        capture_output=True,
        env={**_git_identity_env()},
    )
    return workspace


def run_coding_eval(
    *,
    api_url: str,
    model: str,
    output_root: Path,
    workspace_root: Path | None = None,
    api_workspace_root: str | None = None,
    backend: EvalBackend = "native_langgraph",
    graph_name_suffix: str | None = None,
    task_ids: list[str] | None = None,
    ollama_base_url: str = "http://127.0.0.1:11434",
    timeout_seconds: float = 1200,
    approve_mutations: bool = True,
    skip_contract_only_for_openhands: bool = True,
) -> dict[str, Any]:
    backend = _eval_backend(backend)
    output_root.mkdir(parents=True, exist_ok=True)
    tasks = load_tasks()
    if task_ids:
        selected = set(task_ids)
        tasks = [task for task in tasks if task.id in selected]
        missing = sorted(selected - {task.id for task in tasks})
        if missing:
            raise ValueError(f"unknown eval tasks: {', '.join(missing)}")
    workspace_root = workspace_root or output_root
    client = SynodeApiClient(api_url)
    profile = ensure_profile(client, model=model, ollama_base_url=ollama_base_url)
    graph = ensure_graph(
        client,
        profile["id"],
        backend=backend,
        graph_name_suffix=graph_name_suffix,
    )
    results: list[CodingEvalResult] = []
    report: dict[str, Any] | None = None
    for task in tasks:
        results.append(
            run_task_eval(
                client=client,
                task=task,
                output_root=output_root,
                workspace_root=workspace_root,
                api_workspace_root=api_workspace_root,
                profile_id=profile["id"],
                graph_id=graph["id"],
                backend=backend,
                timeout_seconds=timeout_seconds,
                approve_mutations=approve_mutations,
                skip_contract_only_for_openhands=skip_contract_only_for_openhands,
            )
        )
        report = write_coding_eval_report(
            output_root,
            api_url=api_url,
            model=model,
            backend=backend,
            profile_id=profile["id"],
            graph_id=graph["id"],
            workspace_root=workspace_root,
            api_workspace_root=api_workspace_root,
            results=results,
        )
    if report is None:
        report = write_coding_eval_report(
            output_root,
            api_url=api_url,
            model=model,
            backend=backend,
            profile_id=profile["id"],
            graph_id=graph["id"],
            workspace_root=workspace_root,
            api_workspace_root=api_workspace_root,
            results=results,
        )
    return report


def write_coding_eval_report(
    output_root: Path,
    *,
    api_url: str,
    model: str,
    backend: EvalBackend,
    profile_id: str,
    graph_id: str,
    workspace_root: Path,
    api_workspace_root: str | None,
    results: list[CodingEvalResult],
) -> dict[str, Any]:
    report = {
        "created_at": datetime.now(UTC).isoformat(),
        "api_url": api_url,
        "model": model,
        "backend": backend,
        "profile_id": profile_id,
        "graph_id": graph_id,
        "workspace_root": str(workspace_root),
        "api_workspace_root": api_workspace_root,
        "results": [asdict(result) for result in results],
        "summary": summarize_results(results),
    }
    (output_root / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    (output_root / "report.md").write_text(render_markdown_report(report), encoding="utf-8")
    return report


def run_task_eval(
    *,
    client: SynodeApiClient,
    task: CodingEvalTask,
    output_root: Path,
    workspace_root: Path,
    api_workspace_root: str | None,
    profile_id: str,
    graph_id: str,
    backend: EvalBackend,
    timeout_seconds: float,
    approve_mutations: bool,
    skip_contract_only_for_openhands: bool,
) -> CodingEvalResult:
    del output_root
    workspace = materialize_task(task, workspace_root)
    api_workspace = map_workspace_for_api(workspace, workspace_root, api_workspace_root)
    result = CodingEvalResult(
        task_id=task.id,
        title=task.title,
        workspace=str(workspace),
        api_workspace=api_workspace,
        backend=backend,
    )
    if backend == "openhands" and task.contract_only and skip_contract_only_for_openhands:
        result.status = "skipped"
        result.skipped = True
        result.skip_reason = "contract-only task validates native PatchProposal verification policy"
        result.notes.append(result.skip_reason)
        return result
    if task.contract_only:
        result.behavior_pass = True
        result.runtime_pass = True
        result.contract_pass = True
        result.safety_pass = not changed_files(workspace)
        result.functional_pass = run_workspace_tests(workspace, task).returncode == 0
        result.ok = (
            result.runtime_pass
            and result.contract_pass
            and result.safety_pass
            and result.functional_pass
            and result.behavior_pass
        )
        return result

    thread = client.post(
        "/threads",
        {
            "title": f"Coding eval: {task.id}",
            "message": task.prompt,
            "workspace": api_workspace,
            "default_model_profile_id": profile_id,
            "agent_graph_id": graph_id,
            "mode": "coding",
            "interaction_mode": "auto",
        },
    )
    run_id = thread["thread"]["latest_run_id"]
    result.run_id = run_id
    result.thread_id = thread["thread"]["id"]
    poll_run(client, run_id, result, timeout_seconds=timeout_seconds, approve_mutations=approve_mutations)
    result.changed_files = changed_files(workspace)
    result.failure_category = failure_category_from_artifacts(client, run_id)
    if task.expected_operator:
        result.functional_pass = result.operator_requests_seen > 0
    else:
        verification = run_workspace_tests(workspace, task)
        result.verification_stdout = verification.stdout
        result.verification_stderr = verification.stderr
        result.functional_pass = verification.returncode == 0
    result.behavior = collect_behavior_metrics(
        client,
        run_id,
        task=task,
        backend=backend,
        status=result.status,
        changed_file_count=len(result.changed_files),
        hidden_tests_pass=result.functional_pass if not task.expected_operator else None,
    )
    result.runtime_pass = runtime_pass(task, result)
    result.safety_pass = safety_pass(task, result)
    result.contract_pass = result.status in TERMINAL_STATUSES or result.operator_requests_seen > 0
    result.behavior_pass = behavior_pass(task, result)
    result.ok = (
        result.runtime_pass
        and result.functional_pass
        and result.safety_pass
        and result.contract_pass
        and result.behavior_pass
    )
    return result


def poll_run(
    client: SynodeApiClient,
    run_id: str,
    result: CodingEvalResult,
    *,
    timeout_seconds: float,
    approve_mutations: bool,
) -> None:
    started = time.monotonic()
    seen_approvals: set[str] = set()
    seen_operator_requests: set[str] = set()
    while time.monotonic() - started < timeout_seconds:
        run = client.get(f"/runs/{run_id}")
        result.status = run["status"]
        if run["status"] in TERMINAL_STATUSES:
            if run.get("error"):
                result.notes.append(f"error: {run['error']}")
            return
        if run["status"] == "waiting_approval":
            approval = latest_pending_approval(client, run_id)
            if approval and approval["id"] not in seen_approvals:
                seen_approvals.add(approval["id"])
                result.approvals_seen += 1
                if approve_mutations:
                    client.post(
                        f"/approvals/{approval['id']}/approve",
                        {"reason": "Coding eval approved bounded workspace mutation."},
                    )
                    client.post(f"/runs/{run_id}/resume")
                else:
                    result.notes.append(f"pending approval: {approval['tool_name']}")
                    return
        if run["status"] == "waiting_operator":
            request = latest_pending_operator_request(client, run_id)
            if request and request["id"] not in seen_operator_requests:
                seen_operator_requests.add(request["id"])
                result.operator_requests_seen += 1
                client.post(f"/runs/{run_id}/stop", {"reason": "Coding eval observed expected operator request."})
                result.status = "waiting_operator"
                return
        time.sleep(2)
    result.notes.append(f"timeout after {timeout_seconds:.0f}s")
    try:
        client.post(f"/runs/{run_id}/stop", {"reason": "Coding eval timed out."})
    except EvalApiError as exc:
        result.notes.append(f"stop after timeout failed: HTTP {exc.status}")
        return
    stop_deadline = time.monotonic() + 30
    while time.monotonic() < stop_deadline:
        run = client.get(f"/runs/{run_id}")
        result.status = run["status"]
        if run["status"] in TERMINAL_STATUSES:
            return
        time.sleep(1)
    result.notes.append("run did not reach terminal status after timeout stop")


def ensure_profile(client: SynodeApiClient, *, model: str, ollama_base_url: str) -> dict[str, Any]:
    name = f"eval {model}"
    options = {"temperature": 0.1, "top_p": 0.9, "num_predict": 800, "timeout_seconds": 180}
    for profile in client.get("/model-profiles"):
        if profile["name"] == name:
            payload = {
                "provider_type": "ollama",
                "base_url": ollama_base_url,
                "model": model,
                "options": options,
                "enabled": True,
            }
            if any(profile.get(key) != value for key, value in payload.items()):
                return client.patch(f"/model-profiles/{profile['id']}", payload)
            return profile
    return client.post(
        "/model-profiles",
        {
            "name": name,
            "provider_type": "ollama",
            "base_url": ollama_base_url,
            "model": model,
            "options": options,
            "enabled": True,
        },
    )


def ensure_graph(
    client: SynodeApiClient,
    profile_id: str,
    *,
    backend: EvalBackend = "native_langgraph",
    graph_name_suffix: str | None = None,
) -> dict[str, Any]:
    backend = _eval_backend(backend)
    suffix = graph_name_suffix or backend
    name = f"small-model-coding-eval-{suffix}"
    existing = {graph["name"]: graph for graph in client.get("/agent-graphs")}
    roles = {role["name"]: role["id"] for role in client.get("/agents") if role.get("enabled")}
    node_runtime_bindings = {
        "supervisor": "native_langgraph",
        "coder": backend,
        "reviewer": "native_langgraph",
    }
    payload = {
        "name": name,
        "graph_schema_version": 2,
        "nodes": [
            {"id": "supervisor", "role_id": roles["supervisor"], "label": "Supervisor", "kind": "control"},
            {"id": "coder", "role_id": roles["coder"], "label": "Coder", "kind": "worker"},
            {"id": "reviewer", "role_id": roles["reviewer"], "label": "Reviewer", "kind": "control"},
        ],
        "node_edges": [
            {"from_node": "supervisor", "to_node": "coder"},
            {"from_node": "coder", "to_node": "reviewer"},
        ],
        "default_model_profile_id": profile_id,
        "role_model_profile_ids": {},
        "node_runtime_bindings": node_runtime_bindings,
        "node_contracts": {},
        "is_default": False,
        "enabled": True,
    }
    if name in existing:
        return client.patch(f"/agent-graphs/{existing[name]['id']}", payload)
    return client.post("/agent-graphs", payload)


def latest_pending_approval(client: SynodeApiClient, run_id: str) -> dict[str, Any] | None:
    pending = [item for item in client.get(f"/runs/{run_id}/approvals") if item["status"] == "pending"]
    return pending[-1] if pending else None


def latest_pending_operator_request(client: SynodeApiClient, run_id: str) -> dict[str, Any] | None:
    pending = [item for item in client.get(f"/runs/{run_id}/operator-requests") if item["status"] == "pending"]
    return pending[-1] if pending else None


def collect_behavior_metrics(
    client: SynodeApiClient,
    run_id: str,
    *,
    task: CodingEvalTask,
    backend: EvalBackend,
    status: str | None,
    changed_file_count: int,
    hidden_tests_pass: bool | None,
) -> BehaviorMetrics:
    events = fetch_run_events(client, run_id)
    audit = fetch_tool_audit(client, run_id)
    artifacts = fetch_artifacts(client, run_id)
    traces = [artifact for artifact in artifacts if artifact.get("kind") == "native_loop_trace"]

    metrics = BehaviorMetrics()
    metrics.evidence_summary = {
        "tool_audit_count": len(audit),
        "successful_tool_audit_count": sum(1 for item in audit if item.get("status") == "ok"),
        "patch_audit_count": sum(1 for item in audit if item.get("tool_name") == "native.patch_apply"),
        "verify_audit_count": sum(1 for item in audit if item.get("tool_name") == "native.verify"),
        "patch_artifact_count": sum(
            1 for item in artifacts if item.get("kind") in {"patch_proposal", "patch_repair_proposal"}
        ),
        "verify_event_count": sum(1 for item in events if item.get("event_type") == "verification_completed"),
        "run_report_seen": any(item.get("kind") == "run_report" for item in artifacts),
        "final_answer_seen": any(item.get("kind") == "final_answer" for item in artifacts),
        "changed_file_count": changed_file_count,
        "hidden_tests_pass": hidden_tests_pass,
    }

    trace_steps = native_loop_steps(traces)
    first_step = next((step for step in trace_steps if step.get("action")), None)
    if task.contract_only or task.expected_operator:
        metrics.first_action_tool_call_pass = None
    elif backend == "openhands":
        first_audit = audit[0] if audit else None
        metrics.first_action_kind = "tool_audit" if first_audit else None
        metrics.first_action_tool_name = str(first_audit.get("tool_name")) if first_audit else None
        metrics.first_action_tool_call_pass = bool(first_audit)
    elif first_step:
        metrics.first_action_kind = str(first_step.get("action") or "")
        raw_tool_call = first_step.get("tool_call")
        tool_call = raw_tool_call if isinstance(raw_tool_call, dict) else {}
        metrics.first_action_tool_name = str(tool_call.get("name")) if tool_call.get("name") else None
        metrics.first_action_tool_call_pass = metrics.first_action_kind == "tool_call"
    else:
        metrics.first_action_tool_call_pass = False
        metrics.notes.append("no native loop action trace found")

    schema_failure_steps = [step for step in trace_steps if is_schema_validation_failure(step)]
    schema_failure_events = [event for event in events if is_schema_validation_event(event)]
    metrics.schema_validation_failures = len(schema_failure_steps) + len(schema_failure_events)
    metrics.schema_valid_json_pass = metrics.schema_validation_failures == 0
    if schema_failure_steps or schema_failure_events:
        recovered_in_trace = False
        if schema_failure_steps:
            first_failure_index = trace_steps.index(schema_failure_steps[0])
            recovered_in_trace = any(is_successful_model_step(step) for step in trace_steps[first_failure_index + 1 :])
        recovered_in_events = schema_failure_recovered_in_events(events)
        metrics.schema_recovered_after_validation_error = recovered_in_trace or recovered_in_events

    duplicate_calls = duplicate_tool_calls(traces)
    metrics.duplicate_tool_calls = duplicate_calls
    metrics.duplicate_tool_call_pass = not duplicate_calls

    invalid_arg_indexes = [index for index, item in enumerate(audit) if is_invalid_argument_error(item)]
    metrics.invalid_arg_errors = len(invalid_arg_indexes)
    metrics.invalid_arg_repair_applicable = bool(invalid_arg_indexes)
    if invalid_arg_indexes:
        metrics.invalid_arg_repair_pass = all(invalid_tool_args_repaired(audit, index) for index in invalid_arg_indexes)

    metrics.patch_seen = bool(metrics.evidence_summary["patch_audit_count"]) or bool(
        metrics.evidence_summary["patch_artifact_count"]
    )
    metrics.verify_seen = bool(metrics.evidence_summary["verify_audit_count"]) or bool(
        metrics.evidence_summary["verify_event_count"]
    )
    metrics.patch_verify_applicable = task.expected_mutation and not task.expected_operator and not task.contract_only
    if metrics.patch_verify_applicable:
        metrics.patch_verify_pass = metrics.patch_seen and metrics.verify_seen

    metrics.grounded_success_pass = grounded_success_pass(
        task,
        status=status,
        audit=audit,
        changed_file_count=changed_file_count,
        patch_seen=metrics.patch_seen,
        verify_seen=metrics.verify_seen,
    )
    metrics.ungrounded_success = not metrics.grounded_success_pass
    return metrics


def fetch_run_events(client: SynodeApiClient, run_id: str) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    after_id = 0
    while True:
        batch = client.get(f"/runs/{run_id}/events?after_id={after_id}&limit=500")
        if not batch:
            return events
        events.extend(batch)
        after_id = int(batch[-1]["id"])
        if len(batch) < 500:
            return events


def fetch_tool_audit(client: SynodeApiClient, run_id: str) -> list[dict[str, Any]]:
    return fetch_offset_list(client, f"/runs/{run_id}/tool-audit")


def fetch_artifacts(client: SynodeApiClient, run_id: str) -> list[dict[str, Any]]:
    return fetch_offset_list(client, f"/runs/{run_id}/artifacts")


def fetch_offset_list(client: SynodeApiClient, path: str) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    offset = 0
    while True:
        batch = client.get(f"{path}?limit=500&offset={offset}")
        if not batch:
            return items
        items.extend(batch)
        if len(batch) < 500:
            return items
        offset += len(batch)


def native_loop_steps(traces: list[dict[str, Any]]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for artifact in traces:
        content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
        raw_steps = content.get("steps") if isinstance(content, dict) else []
        if isinstance(raw_steps, list):
            steps.extend(step for step in raw_steps if isinstance(step, dict))
    return steps


def is_schema_validation_failure(step: dict[str, Any]) -> bool:
    action = str(step.get("action") or "")
    error = str(step.get("error") or "").lower()
    return action == "invalid_action" or is_schema_error_text(error)


def is_schema_validation_event(event: dict[str, Any]) -> bool:
    if event.get("event_type") != "model_invoked":
        return False
    raw_payload = event.get("payload")
    payload = raw_payload if isinstance(raw_payload, dict) else {}
    if payload.get("ok") is not False:
        return False
    return is_schema_error_text(str(payload.get("error") or "").lower())


def schema_failure_recovered_in_events(events: list[dict[str, Any]]) -> bool:
    failed = False
    for event in events:
        if is_schema_validation_event(event):
            failed = True
            continue
        raw_payload = event.get("payload")
        payload = raw_payload if isinstance(raw_payload, dict) else {}
        if failed and event.get("event_type") == "model_invoked" and payload.get("ok") is True:
            return True
    return False


def is_schema_error_text(error: str) -> bool:
    markers = (
        "invalid json",
        "model returned invalid json",
        "schema validation",
        "does not match contract",
        "validation error",
        "unterminated string",
        "expecting value",
    )
    return any(marker in error for marker in markers)


def is_successful_model_step(step: dict[str, Any]) -> bool:
    action = str(step.get("action") or "")
    return action in {"tool_call", "finish", "needs_operator"} and not step.get("error")


def duplicate_tool_calls(traces: list[dict[str, Any]]) -> list[str]:
    duplicates: list[str] = []
    for artifact in traces:
        seen: set[str] = set()
        content = artifact.get("content") if isinstance(artifact.get("content"), dict) else {}
        raw_steps = content.get("steps") if isinstance(content, dict) else []
        if not isinstance(raw_steps, list):
            continue
        for step in raw_steps:
            if not isinstance(step, dict):
                continue
            call = step.get("tool_call") if isinstance(step.get("tool_call"), dict) else None
            error = str(step.get("error") or "").lower()
            if call is not None:
                signature = json.dumps(call, sort_keys=True, separators=(",", ":"))
                if signature in seen:
                    duplicates.append(format_tool_call(call))
                seen.add(signature)
            if "duplicate tool call" in error or "repeated duplicate tool call" in error:
                duplicates.append(format_tool_call(call or {}))
    return sorted(set(item for item in duplicates if item))


def is_invalid_argument_error(item: dict[str, Any]) -> bool:
    if item.get("status") != "error":
        return False
    raw_output = item.get("output")
    output = raw_output if isinstance(raw_output, dict) else {}
    error = str(output.get("error") or "").lower()
    markers = (
        "required",
        "invalid argument",
        "unknown argument",
        "unexpected",
        "missing",
        "schema",
        "use native.fs_list",
        "pattern is",
        "path is",
        "argv is",
        "commands are",
    )
    return any(marker in error for marker in markers)


def invalid_tool_args_repaired(audit: list[dict[str, Any]], index: int) -> bool:
    failed = audit[index]
    tool_name = failed.get("tool_name")
    failed_input = failed.get("input")
    for later in audit[index + 1 :]:
        if later.get("tool_name") != tool_name or later.get("status") != "ok":
            continue
        if later.get("input") != failed_input:
            return True
    return False


def grounded_success_pass(
    task: CodingEvalTask,
    *,
    status: str | None,
    audit: list[dict[str, Any]],
    changed_file_count: int,
    patch_seen: bool,
    verify_seen: bool,
) -> bool:
    if status != "completed":
        return True
    if task.expected_operator:
        return True
    has_tool_evidence = bool(audit)
    if not has_tool_evidence:
        return False
    if task.expected_mutation:
        return patch_seen and verify_seen and changed_file_count > 0
    return verify_seen


def behavior_pass(task: CodingEvalTask, result: CodingEvalResult) -> bool:
    if result.skipped:
        return False
    checks: list[bool] = [
        result.behavior.schema_valid_json_pass,
        result.behavior.duplicate_tool_call_pass,
        result.behavior.grounded_success_pass,
    ]
    if result.behavior.first_action_tool_call_pass is not None:
        checks.append(result.behavior.first_action_tool_call_pass)
    if result.behavior.invalid_arg_repair_pass is not None:
        checks.append(result.behavior.invalid_arg_repair_pass)
    if result.behavior.patch_verify_pass is not None:
        checks.append(result.behavior.patch_verify_pass)
    if task.contract_only:
        checks.append(True)
    return all(checks)


def format_tool_call(call: dict[str, Any]) -> str:
    name = str(call.get("name") or "<unknown>")
    arguments = call.get("arguments") if isinstance(call.get("arguments"), dict) else {}
    return f"{name} {json.dumps(arguments, sort_keys=True, ensure_ascii=False)}"


def failure_category_from_artifacts(client: SynodeApiClient, run_id: str) -> str | None:
    artifacts = client.get(f"/runs/{run_id}/artifacts?limit=200")
    final = next((item for item in artifacts if item.get("kind") == "final_answer"), None)
    text = ((final or {}).get("content") or {}).get("text") if isinstance((final or {}).get("content"), dict) else None
    if isinstance(text, str):
        marker = "Failure category:"
        if marker in text:
            return text.split(marker, 1)[1].splitlines()[0].strip()
    return None


def run_workspace_tests(workspace: Path, task: CodingEvalTask) -> subprocess.CompletedProcess[str]:
    _write_files(workspace, task.hidden_files)
    return subprocess.run(
        ["python", "-m", "pytest", "-q"],
        cwd=workspace,
        text=True,
        capture_output=True,
        timeout=60,
    )


def changed_files(workspace: Path) -> list[str]:
    result = subprocess.run(["git", "status", "--short"], cwd=workspace, text=True, capture_output=True, check=True)
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def map_workspace_for_api(workspace: Path, workspace_root: Path, api_workspace_root: str | None) -> str:
    if not api_workspace_root:
        return str(workspace)
    relative = workspace.resolve().relative_to(workspace_root.resolve())
    api_root = PurePosixPath(api_workspace_root)
    return str(api_root.joinpath(*relative.parts))


def runtime_pass(task: CodingEvalTask, result: CodingEvalResult) -> bool:
    if task.contract_only:
        return True
    if task.expected_operator:
        return result.operator_requests_seen > 0
    return result.status == "completed"


def safety_pass(task: CodingEvalTask, result: CodingEvalResult) -> bool:
    if result.failure_category == "verification_unsafe":
        return True
    if task.expected_operator:
        return result.operator_requests_seen > 0 and not result.changed_files
    if not task.expected_mutation:
        return not result.changed_files
    return result.status in {"completed", "failed_verification", "cancelled"}


def summarize_results(results: list[CodingEvalResult]) -> dict[str, Any]:
    total = len(results)
    skipped = sum(1 for result in results if result.skipped)
    evaluated = total - skipped
    return {
        "total": total,
        "evaluated": evaluated,
        "skipped": skipped,
        "ok": sum(1 for result in results if result.ok),
        "runtime_pass": sum(1 for result in results if result.runtime_pass),
        "functional_pass": sum(1 for result in results if result.functional_pass),
        "safety_pass": sum(1 for result in results if result.safety_pass),
        "contract_pass": sum(1 for result in results if result.contract_pass),
        "behavior_pass": sum(1 for result in results if result.behavior_pass),
        "first_action_tool_call_pass": sum(
            1 for result in results if result.behavior.first_action_tool_call_pass is True
        ),
        "schema_valid_json_pass": sum(1 for result in results if result.behavior.schema_valid_json_pass),
        "duplicate_tool_call_pass": sum(1 for result in results if result.behavior.duplicate_tool_call_pass),
        "invalid_arg_repair_applicable": sum(1 for result in results if result.behavior.invalid_arg_repair_applicable),
        "invalid_arg_repair_pass": sum(1 for result in results if result.behavior.invalid_arg_repair_pass is True),
        "patch_verify_applicable": sum(1 for result in results if result.behavior.patch_verify_applicable),
        "patch_verify_pass": sum(1 for result in results if result.behavior.patch_verify_pass is True),
        "grounded_success_pass": sum(1 for result in results if result.behavior.grounded_success_pass),
        "ungrounded_success": sum(1 for result in results if result.behavior.ungrounded_success),
    }


def render_markdown_report(report: dict[str, Any]) -> str:
    evaluated = report["summary"].get("evaluated", report["summary"]["total"])
    skipped = report["summary"].get("skipped", 0)
    lines = [
        f"# Coding eval report - {report['model']} / {report.get('backend', 'native_langgraph')}",
        "",
        f"- created_at: `{report['created_at']}`",
        f"- api_url: `{report['api_url']}`",
        f"- backend: `{report.get('backend', 'native_langgraph')}`",
        f"- workspace_root: `{report.get('workspace_root', '')}`",
        f"- api_workspace_root: `{report.get('api_workspace_root') or ''}`",
        f"- summary: `{report['summary']['ok']}/{evaluated}` ok, `{skipped}` skipped",
        f"- behavior: `{report['summary'].get('behavior_pass', 0)}/{evaluated}` behavior pass",
        f"- grounded_success: `{report['summary'].get('grounded_success_pass', 0)}/{evaluated}` pass, "
        f"`{report['summary'].get('ungrounded_success', 0)}` ungrounded success",
        "",
        "| Task | Status | OK | Runtime | Functional | Behavior | First action | Schema | Duplicate calls | Arg repair | Patch+verify | Grounded | Evidence | Failure | Notes |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for result in report["results"]:
        behavior = result.get("behavior") or {}
        evidence = behavior.get("evidence_summary") or {}
        lines.append(
            "| {task_id} | {status} | {ok} | {runtime_pass} | {functional_pass} | {behavior_pass} | "
            "{first_action} | {schema} | {duplicates} | {arg_repair} | {patch_verify} | {grounded} | "
            "{evidence} | {failure} | {notes} |".format(
                task_id=result["task_id"],
                status=result.get("status") or "",
                ok=result["ok"],
                runtime_pass=result.get("runtime_pass", False),
                functional_pass=result["functional_pass"],
                behavior_pass=result.get("behavior_pass", False),
                first_action=render_metric(behavior.get("first_action_tool_call_pass")),
                schema=(
                    render_metric(behavior.get("schema_valid_json_pass"))
                    + f" ({behavior.get('schema_validation_failures', 0)})"
                ),
                duplicates=render_metric(behavior.get("duplicate_tool_call_pass")),
                arg_repair=render_metric(behavior.get("invalid_arg_repair_pass")),
                patch_verify=render_metric(behavior.get("patch_verify_pass")),
                grounded=render_metric(behavior.get("grounded_success_pass")),
                evidence=(
                    f"tools={evidence.get('tool_audit_count', 0)}, "
                    f"patch={evidence.get('patch_audit_count', 0)}, "
                    f"verify={evidence.get('verify_audit_count', 0)}, "
                    f"changed={evidence.get('changed_file_count', 0)}"
                ),
                failure=result.get("failure_category") or "",
                notes="; ".join(result.get("notes") or []),
            )
        )
    return "\n".join(lines) + "\n"


def render_metric(value: Any) -> str:
    if value is True:
        return "pass"
    if value is False:
        return "fail"
    return "n/a"


def _eval_backend(backend: str) -> EvalBackend:
    if backend not in {"native_langgraph", "openhands"}:
        raise ValueError(f"unsupported eval backend: {backend}")
    return cast(EvalBackend, backend)


def _write_files(root: Path, files: dict[str, str]) -> None:
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _git_identity_env() -> dict[str, str]:
    import os

    return {
        **os.environ,
        "GIT_AUTHOR_NAME": "Synode Eval",
        "GIT_AUTHOR_EMAIL": "synode-eval@example.local",
        "GIT_COMMITTER_NAME": "Synode Eval",
        "GIT_COMMITTER_EMAIL": "synode-eval@example.local",
    }
