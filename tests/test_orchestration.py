from __future__ import annotations

import pathlib
from subprocess import run

import pytest
from sqlalchemy import select

from synode.models.provider import FakeModelProvider, ModelRequest, ModelResponse
from synode.persistence.models import ApprovalRecord
from synode.persistence.repository import Repository
from synode.runtime.decisions import FilePatch, PatchProposal
from synode.runtime.graph import (
    _build_coding_context_packet,
    _normalize_patch_proposal,
    _patch_proposal_validation_errors,
    _route_after_patch_propose,
    _route_after_patch_repair,
    _route_after_verify,
    _select_verification_commands,
    _verification_command_catalog,
)
from synode.runtime.worker import RunWorker
from synode.schemas import (
    ApprovalStatus,
    EventType,
    InteractionMode,
    OperatorRequestDecision,
    OperatorRequestKind,
    OperatorRequestStatus,
    OperatorResponseType,
    RoleName,
    RunMode,
    RunStatus,
)


class StreamingFakeProvider(FakeModelProvider):
    name = "streaming_fake"
    supports_streaming = True

    async def invoke_stream(self, request: ModelRequest, on_delta) -> ModelResponse:
        parts = ["streamed ", "agent ", "output"]
        for part in parts:
            await on_delta(part)
        return ModelResponse(
            content="".join(parts),
            provider=self.name,
            model="streaming-fake",
            input_tokens=1,
            output_tokens=3,
            total_tokens=4,
            latency_ms=1.0,
        )


def test_route_after_verify_allows_one_real_model_repair_pass() -> None:
    assert (
        _route_after_verify(
            {
                "mode": RunMode.CODING.value,
                "model_provider": "ollama",
                "verification_result": {"ok": False},
                "coding_repair_attempts": 0,
            }
        )
        == "coding_patch_repair"
    )
    assert (
        _route_after_verify(
            {
                "mode": RunMode.CODING.value,
                "model_provider": "ollama",
                "verification_result": {"ok": False},
                "coding_repair_attempts": 1,
            }
        )
        == "coding_patch_repair"
    )
    assert (
        _route_after_verify(
            {
                "mode": RunMode.CODING.value,
                "model_provider": "fake",
                "verification_result": {"ok": False},
                "coding_repair_attempts": 0,
            }
        )
        == "reviewer"
    )
    assert (
        _route_after_verify(
            {
                "mode": RunMode.CODING.value,
                "model_provider": "ollama",
                "verification_result": {"ok": False},
                "coding_repair_attempts": 2,
            }
        )
        == "reviewer"
    )


def test_route_after_patch_repair_falls_back_to_review_on_repair_error() -> None:
    assert _route_after_patch_repair({"patch_repair_error": "invalid repair proposal"}) == "reviewer"
    assert _route_after_patch_repair({"coding_action": "no_change"}) == "verify"
    assert _route_after_patch_repair({"patch_proposal": {"patches": []}}) == "patch_apply"


def test_route_after_patch_propose_handles_action_states() -> None:
    assert _route_after_patch_propose({"coding_action": "patch"}) == "patch_apply"
    assert _route_after_patch_propose({"coding_action": "no_change"}) == "verify"
    assert _route_after_patch_propose({"coding_failure_category": "needs_operator"}) == "reviewer"
    assert _route_after_patch_propose({"patch_repair_error": "invalid"}) == "reviewer"


def test_patch_proposal_normalizer_aligns_unique_indented_old_text() -> None:
    content = "def total(rows):\n    for row in rows:\n        value += row.amount\n    return value\n"
    proposal = PatchProposal(
        summary="Patch total.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="0" * 64,
                old_text="for row in rows:\nvalue += row.amount",
                new_text="for row in rows:\n        value -= row.amount",
            )
        ],
        verification_commands=[["pytest", "-q"]],
    )

    normalized = _normalize_patch_proposal(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
    )

    assert normalized.patches[0].expected_sha256 == "1" * 64
    assert normalized.patches[0].old_text == "    for row in rows:\n        value += row.amount"


def test_patch_proposal_validation_rejects_unsafe_verification_command() -> None:
    content = "def total(rows):\n    return sum(row.amount for row in rows)\n"
    proposal = PatchProposal(
        summary="Patch total.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="return sum(row.amount for row in rows)",
                new_text="return sum(row.net_amount for row in rows)",
            )
        ],
        verification_commands=[["git", "add", "."]],
    )

    errors = _patch_proposal_validation_errors(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
    )

    assert "verification command 0 is unsafe: ['git', 'add', '.']" in errors


def test_patch_proposal_validation_rejects_commands_outside_catalog() -> None:
    content = "def total(rows):\n    return sum(row.amount for row in rows)\n"
    proposal = PatchProposal(
        summary="Patch total.",
        patches=[
            FilePatch(
                path="ledger.py",
                expected_sha256="1" * 64,
                old_text="return sum(row.amount for row in rows)",
                new_text="return sum(row.net_amount for row in rows)",
            )
        ],
        verification_commands=[["python", "-m", "pytest"]],
    )

    errors = _patch_proposal_validation_errors(
        proposal,
        [{"path": "ledger.py", "sha256": "1" * 64, "content": content}],
        allowed_verification_commands=[["pytest", "-q"]],
    )

    assert "verification command 0 is not in allowed command catalog: ['python', '-m', 'pytest']" in errors


def test_no_change_patch_proposal_uses_verification_without_patches() -> None:
    proposal = PatchProposal(
        action="no_change",
        summary="Already correct.",
        verification_commands=[["pytest", "-q"]],
    )

    assert proposal.patches == []
    assert _select_verification_commands(
        proposal,
        {"coding_context_packet": {"allowed_verification_commands": [["pytest", "-q"]]}},
    ) == [["pytest", "-q"]]


def test_verification_catalog_and_context_packet_are_compact(settings) -> None:
    catalog = _verification_command_catalog(
        {"task": "Run pytest after the fix."},
        {"relevant_files": ["tests/test_demo.py"], "proposed_test_commands": [["git", "add", "."]]},
    )

    assert catalog == [["pytest", "-q"], ["python", "-m", "pytest"]]

    class FakeToolExecutor:
        def __init__(self) -> None:
            self.settings = settings

    class FakeDeps:
        def __init__(self) -> None:
            self.tool_executor = FakeToolExecutor()

    packet = _build_coding_context_packet(
        FakeDeps(),
        {
            "run_id": "run",
            "thread_id": "thread",
            "task": "Fix demo.",
            "coding_inspection": {"observed_failures": ["failed"]},
        },
        file_context=[
            {
                "path": "demo.py",
                "sha256": "1" * 64,
                "content": "def demo():\n    return 1\n",
            }
        ],
        allowed_commands=catalog,
        repair_verification=False,
        extra_context={},
    )

    assert packet["allowed_verification_commands"] == catalog
    assert packet["files"][0]["path"] == "demo.py"


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
    first_events = await service.list_event_responses(first.id, limit=500)
    assert any(event.event_type == EventType.APPROVAL_REQUIRED for event in first_events)
    assert not any(
        event.event_type == EventType.NODE_STARTED and event.role == RoleName.REVIEWER
        for event in first_events
    )
    assert not any(event.event_type == EventType.RUN_COMPLETED for event in first_events)
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
    assert await RunWorker(service, worker_id="approval-worker").run_once() is True
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
    assert await RunWorker(service, worker_id="approval-worker").run_once() is True
    second = await service.get_run(first.id)

    assert second.status == RunStatus.FAILED_VERIFICATION
    assert second.final_answer is not None
    assert "verification failed" in second.final_answer


async def test_rejecting_approval_cancels_run_and_unblocks_thread(
    service, database, tmp_path: pathlib.Path
) -> None:
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

    await service.reject(approval.id, "not this patch")
    rejected = await service.get_run(first.id)
    follow_up = await service.create_thread_run(
        rejected.thread_id,
        "Continue with a different approach",
        workspace=str(tmp_path),
        model_provider="fake",
    )

    assert rejected.status == RunStatus.CANCELLED
    assert rejected.final_answer == "Approval rejected for native.patch_apply."
    assert follow_up.status == RunStatus.CREATED
    with pytest.raises(ValueError, match="run is terminal"):
        await service.resume_run(first.id)


async def test_follow_up_run_receives_thread_conversation_context(
    service,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "data.csv").write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")
    first = await service.run_task(
        "First question about the dataset",
        workspace=str(tmp_path),
        model_provider="fake",
    )
    follow_up = await service.create_thread_run(
        first.thread_id,
        "Use that context for a follow-up",
        workspace=str(tmp_path),
        model_provider="fake",
    )
    captured_state: dict[str, object] = {}

    async def capture_graph(run_id: str, state: dict[str, object]) -> dict[str, object]:
        captured_state.update(state)
        return {"review": {"can_proceed": True}, "final_answer": "done"}

    monkeypatch.setattr(service, "_invoke_graph", capture_graph)

    await service.execute_run(follow_up.id)

    context = captured_state["conversation_context"]
    assert isinstance(context, list)
    assert captured_state["thread_id"] == first.thread_id
    assert any(item["author_type"] == "user" and item["content"] == "First question about the dataset" for item in context)
    assert any(item["message_type"] == "final" for item in context)
    assert not any(item["message_type"] == "run_summary" for item in context)
    assert not any(item["author_type"] == "user" and item["content"] == "Use that context for a follow-up" for item in context)


async def test_plan_review_waits_for_operator_then_resumes(service, tmp_path: pathlib.Path) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n2026-06-02,20\n", encoding="utf-8")

    first = await service.run_task(
        "Analyze sample data and summarize findings",
        workspace=str(tmp_path),
        model_provider="fake",
        interaction_mode=InteractionMode.PLAN_REVIEW,
    )
    requests = await service.list_operator_requests(run_id=first.id)

    assert first.status == RunStatus.WAITING_OPERATOR
    assert len(requests) == 1
    assert requests[0].kind == OperatorRequestKind.PLAN_REVIEW
    assert requests[0].status == OperatorRequestStatus.PENDING
    assert requests[0].proposed_payload["decision"]["selected_roles"] == ["data_analyst"]

    await service.respond_operator_request(
        requests[0].id,
        OperatorRequestDecision(response_type=OperatorResponseType.APPROVE),
    )
    assert await RunWorker(service, worker_id="operator-approval-worker").run_once() is True
    resumed = await service.get_run(first.id)
    updated_requests = await service.list_operator_requests(run_id=first.id)

    assert resumed.status == RunStatus.COMPLETED
    assert resumed.final_answer is not None
    assert "data_analyst" in resumed.final_answer
    assert updated_requests[0].status == OperatorRequestStatus.RESOLVED
    assert updated_requests[0].consumed_at is not None


async def test_plan_review_reject_cancels_run(service, tmp_path: pathlib.Path) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n", encoding="utf-8")
    first = await service.run_task(
        "Analyze sample data",
        workspace=str(tmp_path),
        model_provider="fake",
        interaction_mode=InteractionMode.PLAN_REVIEW,
    )
    request = (await service.list_operator_requests(run_id=first.id))[0]

    await service.respond_operator_request(
        request.id,
        OperatorRequestDecision(response_type=OperatorResponseType.REJECT, message="plan needs changes"),
    )
    rejected = await service.get_run(first.id)

    assert rejected.status == RunStatus.CANCELLED
    assert rejected.final_answer == "plan needs changes"


async def test_plan_only_finishes_without_worker_tools(service, tmp_path: pathlib.Path) -> None:
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n", encoding="utf-8")

    result = await service.run_task(
        "Plan the sample data analysis",
        workspace=str(tmp_path),
        model_provider="fake",
        interaction_mode=InteractionMode.PLAN_ONLY,
    )
    audit = await service.list_tool_audit(result.id)

    assert result.status == RunStatus.COMPLETED
    assert result.final_answer is not None
    assert "- data_analyst:" in result.final_answer
    assert audit == []


async def test_streaming_provider_emits_model_stream_events(service, tmp_path: pathlib.Path) -> None:
    service.models.register(StreamingFakeProvider())
    (tmp_path / "data.csv").write_text("date,revenue\n2026-06-01,10\n", encoding="utf-8")

    result = await service.run_task(
        "Analyze sample data and summarize findings",
        workspace=str(tmp_path),
        model_provider="streaming_fake",
    )
    events = await service.list_event_responses(result.id, limit=500)
    event_types = [event.event_type for event in events]
    deltas = [
        event.payload["delta"]
        for event in events
        if event.event_type == EventType.MODEL_TOKEN_DELTA.value
    ]

    assert result.status == RunStatus.COMPLETED
    assert EventType.MODEL_STREAM_STARTED.value in event_types
    assert EventType.MODEL_STREAM_COMPLETED.value in event_types
    assert "".join(deltas) == "streamed agent output"
    assert any(
        event.event_type == EventType.MODEL_INVOKED.value
        and event.payload["provider"] == "streaming_fake"
        for event in events
    )


async def test_stop_created_run_cancels_and_unblocks_thread(service, tmp_path: pathlib.Path) -> None:
    run_response = await service.create_run(
        "Inspect the workspace",
        workspace=str(tmp_path),
        model_provider="fake",
    )

    stopped = await service.stop_run(run_response.id, "No longer needed")
    follow_up = await service.create_thread_run(
        stopped.thread_id,
        "Continue with a new request",
        workspace=str(tmp_path),
        model_provider="fake",
    )

    assert stopped.status == RunStatus.CANCELLED
    assert stopped.final_answer == "No longer needed"
    assert follow_up.status == RunStatus.CREATED


async def test_run_metrics_are_not_limited_by_event_page_size(service, database, tmp_path: pathlib.Path) -> None:
    run_response = await service.create_run(
        "Collect metric events",
        workspace=str(tmp_path),
        model_provider="fake",
    )
    async with database.session() as session:
        repo = Repository(session)
        for _ in range(205):
            await repo.add_event(
                run_response.id,
                EventType.MODEL_INVOKED.value,
                "tester",
                {
                    "provider": "fake",
                    "role": "tester",
                    "usage": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                    "latency_ms": 1,
                },
            )

    metrics = await service.run_metrics(run_response.id)

    assert metrics.event_count == 206
    assert metrics.model_call_count == 205
    assert metrics.token_usage.total_tokens == 615
    assert metrics.latency_ms_by_role["tester"] == 205
