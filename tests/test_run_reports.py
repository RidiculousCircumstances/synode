from __future__ import annotations

from synode.application.reports import build_run_report


def test_run_report_builder_renders_patch_and_verification_without_raw_summary_protocol() -> None:
    report = build_run_report(
        {
            "run_id": "run-1",
            "thread_id": "thread-1",
            "mode": "coding",
            "plan": [{"role": "coder", "task": "Update refund totals"}],
            "worker_outputs": [
                {
                    "role": "coder",
                    "summary": "Updated refund calculation.",
                    "tool_results": [{"tool_name": "native.fs_read", "ok": True, "risk": "read", "output": {"path": "ledger.py"}}],
                }
            ],
            "patch_results": [
                {
                    "tool_name": "native.patch_apply",
                    "ok": True,
                    "risk": "write",
                    "output": {"files": [{"path": "ledger.py", "operation": "modified", "summary": "Refund total uses signed values."}]},
                }
            ],
            "verification_result": {
                "ok": True,
                "commands": [{"command": "pytest", "status": "passed", "stdout": "1 passed"}],
            },
            "review": {"can_proceed": True, "blockers": [], "advisory_risks": []},
        },
        status="completed",
    )

    assert report.headline == "Changes applied and verified"
    assert report.patch_results.status == "ok"
    assert report.patch_results.files[0].path == "ledger.py"
    assert report.verification.status == "passed"
    assert "Synode run summary:" not in report.chat_text()

