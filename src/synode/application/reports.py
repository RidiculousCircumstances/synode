from __future__ import annotations

from typing import Any, Literal

from synode.domain.reports import (
    PatchFileReport,
    PatchResultsReport,
    PlanReportStep,
    RoleOutputReport,
    RunReport,
    ToolActivityReport,
    VerificationCommandReport,
    VerificationReport,
)


def build_run_report(state: dict[str, Any], *, status: str = "completed") -> RunReport:
    review = _dict(state.get("review"))
    blockers = _strings(review.get("blockers"))
    advisory = _strings(review.get("advisory_risks"))
    role_outputs = [_role_output(item) for item in _dicts(state.get("worker_outputs"))]
    patch_results = _patch_results(_dicts(state.get("patch_results")))
    verification = _verification(_dict(state.get("verification_result")))
    headline = _headline(state, status=status, blockers=blockers, patch_results=patch_results, verification=verification)

    return RunReport(
        run_id=str(state.get("run_id") or ""),
        thread_id=str(state.get("thread_id") or ""),
        mode=str(state.get("mode") or "general"),
        status=status,
        headline=headline,
        summary=_summary(role_outputs, patch_results, verification, blockers),
        plan=[_plan_step(item) for item in _dicts(state.get("plan"))],
        role_outputs=role_outputs,
        patch_results=patch_results,
        verification=verification,
        tool_activity=_tool_activity(state),
        blockers=blockers,
        advisory=advisory,
        diagnostics=_diagnostics(state),
    )


def _headline(
    state: dict[str, Any],
    *,
    status: str,
    blockers: list[str],
    patch_results: PatchResultsReport,
    verification: VerificationReport,
) -> str:
    if blockers:
        return "Run needs attention"
    if status in {"failed", "failed_verification", "cancelled"}:
        return f"Run {status.replace('_', ' ')}"
    if patch_results.status == "ok" and verification.status == "passed":
        return "Changes applied and verified"
    if patch_results.status == "no_change":
        return "No code changes were needed"
    if verification.status == "passed":
        return "Run completed and verified"
    if state.get("plan_only"):
        return "Plan is ready for review"
    return "Run completed"


def _summary(
    role_outputs: list[RoleOutputReport],
    patch_results: PatchResultsReport,
    verification: VerificationReport,
    blockers: list[str],
) -> str:
    if blockers:
        return blockers[0]
    if role_outputs:
        return role_outputs[-1].summary
    if patch_results.status != "not_applicable":
        return f"Patch status: {patch_results.status.replace('_', ' ')}"
    if verification.status != "not_run":
        return f"Verification status: {verification.status}"
    return "The workflow finished without a detailed role summary."


def _plan_step(item: dict[str, Any]) -> PlanReportStep:
    tools = item.get("tool_calls")
    return PlanReportStep(
        role=str(item.get("role") or "agent"),
        task=str(item.get("task") or ""),
        status=str(item.get("status") or "planned"),  # type: ignore[arg-type]
        tool_count=len(tools) if isinstance(tools, list) else 0,
    )


def _role_output(item: dict[str, Any]) -> RoleOutputReport:
    tool_results = _dicts(item.get("tool_results"))
    return RoleOutputReport(
        role=str(item.get("role") or "agent"),
        summary=str(item.get("summary") or ""),
        tool_count=len(tool_results),
        failed_tool_count=sum(1 for result in tool_results if not result.get("ok")),
        risks=_strings(item.get("risks")),
    )


def _patch_results(results: list[dict[str, Any]]) -> PatchResultsReport:
    if not results:
        return PatchResultsReport()
    files: list[PatchFileReport] = []
    has_failure = False
    has_pending = False
    no_change = False
    for result in results:
        ok = bool(result.get("ok"))
        approval_id = result.get("approval_id")
        output = _dict(result.get("output"))
        error = str(result.get("error")) if result.get("error") else None
        if approval_id:
            has_pending = True
        if not ok and not approval_id:
            has_failure = True
        no_change = no_change or bool(output.get("no_change"))
        for file_item in _extract_patch_files(output):
            files.append(file_item)
        if not files:
            target = output.get("path") or output.get("file") or result.get("tool_name") or "patch"
            files.append(
                PatchFileReport(
                    path=str(target),
                    operation=str(output.get("operation") or "modified"),
                    status=_patch_file_status(ok=ok, approval_id=approval_id, no_change=no_change),
                    summary=_short_text(output.get("summary") or output.get("stdout")),
                    error=error,
                )
            )
    status = "pending_approval" if has_pending else "failed" if has_failure else "no_change" if no_change else "ok"
    return PatchResultsReport(status=status, files=files, raw_count=len(results))  # type: ignore[arg-type]


def _extract_patch_files(output: dict[str, Any]) -> list[PatchFileReport]:
    candidates = output.get("files") or output.get("changed_files") or output.get("patches")
    if not isinstance(candidates, list):
        return []
    files: list[PatchFileReport] = []
    for item in candidates:
        if isinstance(item, str):
            files.append(PatchFileReport(path=item))
        elif isinstance(item, dict):
            path = item.get("path") or item.get("file") or item.get("target")
            if path:
                files.append(
                    PatchFileReport(
                        path=str(path),
                        operation=str(item.get("operation") or item.get("action") or "modified"),
                        summary=_short_text(item.get("summary") or item.get("reason")),
                    )
                )
    return files


def _patch_file_status(
    *,
    ok: bool,
    approval_id: object,
    no_change: bool,
) -> Literal["ok", "failed", "pending_approval", "skipped"]:
    if approval_id:
        return "pending_approval"
    if no_change:
        return "skipped"
    return "ok" if ok else "failed"


def _verification(raw: dict[str, Any]) -> VerificationReport:
    if not raw:
        return VerificationReport()
    if raw.get("skipped"):
        status = "skipped"
    elif raw.get("ok") is True:
        status = "passed"
    elif raw.get("ok") is False:
        status = "failed"
    else:
        status = "not_run"
    commands = []
    for item in _dicts(raw.get("commands")):
        command = str(item.get("command") or item.get("cmd") or "")
        if not command:
            continue
        commands.append(
            VerificationCommandReport(
                command=command,
                status=_command_status(item),
                summary=_short_text(item.get("summary") or item.get("stdout") or item.get("stderr")),
            )
        )
    return VerificationReport(status=status, commands=commands, reason=_short_text(raw.get("reason") or raw.get("error")))  # type: ignore[arg-type]


def _command_status(item: dict[str, Any]) -> Literal["passed", "failed", "skipped", "unknown"]:
    status = item.get("status")
    if status == "passed":
        return "passed"
    if status == "failed":
        return "failed"
    if status == "skipped":
        return "skipped"
    ok = item.get("ok")
    if ok is True:
        return "passed"
    if ok is False:
        return "failed"
    return "unknown"


def _tool_activity(state: dict[str, Any]) -> list[ToolActivityReport]:
    activity: list[ToolActivityReport] = []
    for output in _dicts(state.get("worker_outputs")):
        role = str(output.get("role") or "agent")
        for result in _dicts(output.get("tool_results")):
            activity.append(_tool_result_activity(result, role=role))
    for result in _dicts(state.get("patch_results")):
        activity.append(_tool_result_activity(result, role="coder"))
    return activity


def _tool_result_activity(result: dict[str, Any], *, role: str | None) -> ToolActivityReport:
    tool = str(result.get("tool_name") or "tool")
    status = "approval_required" if result.get("approval_id") else "ok" if result.get("ok") else "failed"
    target = _tool_target(_dict(result.get("output")))
    return ToolActivityReport(
        role=role,
        tool_name=tool,
        status=status,
        risk=str(result.get("risk")) if result.get("risk") else None,
        title=f"{tool} {status.replace('_', ' ')}",
        target=target,
        approval_id=str(result.get("approval_id")) if result.get("approval_id") else None,
    )


def _tool_target(output: dict[str, Any]) -> str | None:
    for key in ("path", "file", "command", "query", "url"):
        value = output.get(key)
        if value:
            return _short_text(value, limit=160)
    return None


def _diagnostics(state: dict[str, Any]) -> dict[str, Any]:
    keys = ("coding_failure_category", "coding_repair_attempts", "patch_repair_error")
    return {key: state[key] for key in keys if state.get(key) is not None}


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dicts(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _strings(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _short_text(value: Any, *, limit: int = 260) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."
