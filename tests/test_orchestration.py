from __future__ import annotations

import pathlib
from subprocess import run

import pytest
from sqlalchemy import select

from synode.models.errors import StructuredOutputValidationError
from synode.models.provider import (
    FakeModelProvider,
    ModelProviderRegistry,
    ModelRequest,
    ModelResponse,
)
from synode.observability import Observability
from synode.persistence.models import ApprovalRecord
from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.runtime.contracts import WORKER_AGENT_OUTPUT_CONTRACT
from synode.runtime.decisions import NativeLoopAction, PatchProposal, SupervisorDecision
from synode.runtime.graph import (
    GraphDependencies,
    ResolvedModelProvider,
    _build_coding_context_packet,
    _fallback_coding_inspection_from_loop_error,
    _invalid_operator_request_reason,
    _invoke_model,
    _invoke_structured,
    _patch_proposal_prompt,
    _route_after_coding_inspect,
    _route_after_patch_propose,
    _route_after_patch_repair,
    _route_after_verify,
    _run_native_loop,
    _sanitize_supervisor_decision,
    _select_verification_commands,
    _validate_supervisor_decision,
    _verification_command_catalog,
)
from synode.runtime.operator import ApprovalRequired
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
    ToolCall,
    ToolResult,
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


class ScriptedLoopProvider(FakeModelProvider):
    name = "scripted_loop"
    supports_streaming = False

    def __init__(self, actions: list[dict[str, object]]) -> None:
        self.actions = actions
        self.index = 0
        self.requests: list[ModelRequest] = []

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        if self.index >= len(self.actions):
            action = self.actions[-1]
        else:
            action = self.actions[self.index]
            self.index += 1
        return ModelResponse(
            content="{}",
            structured=action,
            provider=self.name,
            model="scripted-loop",
            input_tokens=0,
            output_tokens=0,
            total_tokens=0,
            latency_ms=0.0,
        )


class StructuredRetryFakeProvider(FakeModelProvider):
    name = "structured_retry_fake"
    supports_streaming = False

    def __init__(self) -> None:
        self.calls = 0
        self.prompts: list[str] = []

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        self.calls += 1
        self.prompts.append(request.prompt)
        if self.calls == 1:
            raise StructuredOutputValidationError("model returned invalid JSON: truncated")
        return await super().invoke(request)


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


def test_route_after_coding_inspect_handles_contract_failure() -> None:
    assert _route_after_coding_inspect({"coding_failure_category": "contract_invalid"}) == "reviewer"
    assert _route_after_coding_inspect({"coding_inspection": {"summary": "ok"}}) == "coding_patch_propose"


def test_native_loop_action_accepts_needs_operator_question_alias() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "needs_operator",
            "summary": "Need clarification.",
            "question": "Which behavior should be implemented?",
        }
    )

    assert action.operator_question == "Which behavior should be implemented?"


def test_native_loop_action_uses_summary_for_needs_operator_question() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "needs_operator",
            "summary": "Need operator intent to proceed.",
        }
    )

    assert action.operator_question == "Need operator intent to proceed."


def test_patch_proposal_accepts_needs_operator_question_alias() -> None:
    proposal = PatchProposal.model_validate(
        {
            "action": "needs_operator",
            "summary": "Ambiguous behavior.",
            "prompt": "Should refunds affect revenue and customer totals?",
        }
    )

    assert proposal.operator_question == "Should refunds affect revenue and customer totals?"


def test_native_loop_action_wraps_top_level_finish_payload() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "finish",
            "summary": "Propose a patch.",
            "patches": [],
            "verification_commands": [["pytest", "-q"]],
        }
    )

    assert action.payload == {"patches": [], "verification_commands": [["pytest", "-q"]]}


def test_native_loop_action_wraps_proposal_finish_payload() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "finish",
            "summary": "Propose a patch.",
            "proposal": {
                "action": "no_change",
                "summary": "Already correct.",
                "verification_commands": [["pytest", "-q"]],
            },
        }
    )

    assert action.payload["action"] == "no_change"


def test_native_loop_action_wraps_flat_tool_call_payload() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "tool_call",
            "summary": "List Python files.",
            "name": "native.fs_list",
            "payload": {"glob": "*.py"},
        }
    )

    assert action.tool_call is not None
    assert action.tool_call.name == "native.fs_list"
    assert action.tool_call.arguments == {"glob": "*.py"}


def test_native_loop_action_wraps_flat_tool_call_tool_name_payload() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "tool_call",
            "summary": "List Python files.",
            "tool_name": "native.fs_list",
            "payload": {"glob": "*.py"},
        }
    )

    assert action.tool_call is not None
    assert action.tool_call.name == "native.fs_list"
    assert action.tool_call.arguments == {"glob": "*.py"}


def test_native_loop_action_wraps_flat_tool_call_arguments() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "tool_call",
            "summary": "List Python files.",
            "tool_name": "native.fs_list",
            "arguments": {"glob": "*.py"},
        }
    )

    assert action.tool_call is not None
    assert action.tool_call.name == "native.fs_list"
    assert action.tool_call.arguments == {"glob": "*.py"}


def test_native_loop_action_wraps_payload_tool_name_arguments() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "tool_call",
            "summary": "List Python files.",
            "payload": {"tool_name": "native.fs_list", "arguments": {"glob": "*.py"}},
        }
    )

    assert action.tool_call is not None
    assert action.tool_call.name == "native.fs_list"
    assert action.tool_call.arguments == {"glob": "*.py"}


def test_native_loop_action_infers_fs_list_from_glob_payload() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "tool_call",
            "summary": "List Python files.",
            "payload": {"glob": "*.py"},
        }
    )

    assert action.tool_call is not None
    assert action.tool_call.name == "native.fs_list"
    assert action.tool_call.arguments == {"glob": "*.py"}


def test_native_loop_action_infers_fs_search_from_pattern_payload() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "tool_call",
            "summary": "Search refund logic.",
            "payload": {"pattern": "refund|sale", "glob": "*.py"},
        }
    )

    assert action.tool_call is not None
    assert action.tool_call.name == "native.fs_search"
    assert action.tool_call.arguments == {"pattern": "refund|sale", "glob": "*.py"}


def test_native_loop_action_schema_allows_flat_tool_aliases() -> None:
    schema = NativeLoopAction.model_json_schema()
    tool_schema = next(value for key, value in schema["$defs"].items() if key.endswith("ToolCallAction"))

    assert "tool_call" in tool_schema["required"]
    assert "tool_name" in tool_schema["properties"]
    assert "arguments" in tool_schema["properties"]


def test_native_loop_action_schema_requires_finish_payload_for_local_models() -> None:
    schema = NativeLoopAction.model_json_schema()
    finish_schema = next(value for key, value in schema["$defs"].items() if key.endswith("FinishAction"))

    assert "payload" in finish_schema["required"]
    assert finish_schema["properties"]["payload"]["minProperties"] == 1


def test_patch_proposal_prompt_uses_direct_patch_proposal_terms() -> None:
    prompt = _patch_proposal_prompt(repair=False)

    assert "Return one PatchProposal JSON object directly" in prompt
    assert "Choose exactly one PatchProposal action: patch, no_change, or needs_operator" in prompt
    assert "outer action=finish" not in prompt


def test_patch_operator_request_rejects_delegated_work() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "needs_operator",
            "summary": "Delegate work",
            "operator_question": (
                "Please review the net_revenue_by_month function, make the necessary changes, "
                "and run the tests."
            ),
        }
    )

    assert _invalid_operator_request_reason(action, contract_id="coding_patch_proposal") is not None


def test_patch_operator_request_allows_specific_question() -> None:
    action = NativeLoopAction.model_validate(
        {
            "action": "needs_operator",
            "summary": "Refund policy ambiguity",
            "operator_question": "Should refunds without matching sales create negative monthly revenue?",
        }
    )

    assert _invalid_operator_request_reason(action, contract_id="coding_patch_proposal") is None


def test_fallback_coding_inspection_extracts_relative_files_from_tool_results() -> None:
    inspection = _fallback_coding_inspection_from_loop_error(
        {
            "workspace": "/workspace/project",
            "task": "Run pytest -q after the patch.",
        },
        "native loop repeated duplicate tool call after feedback: native.fs_search",
        [
            ToolResult(
                tool_name="native.fs_read",
                ok=True,
                output={"path": "/workspace/project/ledger_app/ledger.py", "content": "def total(): ..."},
            ),
            ToolResult(
                tool_name="native.fs_search",
                ok=True,
                output={"matches": [{"path": "tests/test_ledger.py"}]},
            ),
        ],
    )

    assert inspection.relevant_files == ["ledger_app/ledger.py", "tests/test_ledger.py"]
    assert inspection.proposed_test_commands == [["pytest", "-q"]]


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
    artifacts = await service.list_artifacts(result.id)
    loop_traces = [artifact for artifact in artifacts if artifact.kind == "native_loop_trace"]
    assert any(trace.content["role"] == RoleName.DATA_ANALYST for trace in loop_traces)
    assert any(
        step.get("action") == "tool_call" and step.get("tool_call", {}).get("name") == "native.data_profile"
        for trace in loop_traces
        for step in trace.content["steps"]
    )


async def test_supervisor_plan_sanitizer_drops_disallowed_tool_hints(
    settings,
    database,
    tool_executor,
) -> None:
    roles = RoleRegistry.load_builtin()
    deps = GraphDependencies(
        database=database,
        roles=roles,
        models=ModelProviderRegistry(),
        tool_executor=tool_executor,
        observability=Observability(settings),
    )
    decision = SupervisorDecision(
        selected_roles=["coder"],
        plan=[
            {
                "role": "coder",
                "task": "Inspect and patch the repository.",
                "tool_calls": [
                    ToolCall(name="native.data_profile", arguments={}),
                    ToolCall(name="native.fs_list", arguments={}),
                ],
            }
        ],
        confidence="medium",
        risk_level="small-code",
        reasoning_summary="Scripted invalid tool hint.",
    )

    sanitized, dropped = _sanitize_supervisor_decision(decision, deps, {"run_id": "run-1"})

    assert [call.name for call in sanitized.plan[0].tool_calls] == ["native.fs_list"]
    assert dropped == [
        {
            "role": "coder",
            "tool_name": "native.data_profile",
            "reason": "role_not_allowed",
            "task": "Inspect and patch the repository.",
        }
    ]
    _validate_supervisor_decision(sanitized, deps, {"run_id": "run-1"})


async def test_native_worker_loop_rejects_disallowed_tool_without_execution(
    settings,
    database,
    tool_executor,
    tmp_path: pathlib.Path,
) -> None:
    models = ModelProviderRegistry()
    models.register(
        ScriptedLoopProvider(
            [
                {
                    "action": "tool_call",
                    "summary": "Try a disallowed write.",
                    "tool_call": {"name": "native.fs_write", "arguments": {"path": "blocked.txt", "content": "no"}},
                },
                {
                    "action": "finish",
                    "summary": "Finish after policy feedback.",
                    "payload": {
                        "role": "data_analyst",
                        "summary": "Finished without write.",
                        "tool_results": [],
                        "risks": [],
                    },
                },
            ]
        )
    )
    deps = GraphDependencies(
        database=database,
        roles=tool_executor.roles,
        models=models,
        tool_executor=tool_executor,
        observability=Observability(settings),
    )

    result = await _run_native_loop(
        deps,
        {
            "run_id": "run-disallowed",
            "thread_id": "thread-disallowed",
            "task": "Analyze only.",
            "mode": RunMode.GENERAL.value,
            "model_provider": "scripted_loop",
            "workspace": str(tmp_path),
        },
        role=RoleName.DATA_ANALYST.value,
        node_id="data_analyst",
        contract_id=WORKER_AGENT_OUTPUT_CONTRACT,
        prompt="Analyze without mutation.",
        context={},
    )

    assert result.payload["summary"] == "Finished without write."
    assert result.tool_results == []
    assert not (tmp_path / "blocked.txt").exists()
    async with database.session() as session:
        artifacts = await Repository(session).list_artifacts("run-disallowed")
    loop_traces = [artifact for artifact in artifacts if artifact.kind == "native_loop_trace"]
    assert loop_traces
    assert "tool is not allowed" in loop_traces[-1].content["steps"][0]["error"]


async def test_native_worker_loop_surfaces_approval(
    settings,
    database,
    tool_executor,
    tmp_path: pathlib.Path,
) -> None:
    models = ModelProviderRegistry()
    models.register(
        ScriptedLoopProvider(
            [
                {
                    "action": "tool_call",
                    "summary": "Request a governed write.",
                    "tool_call": {"name": "native.fs_write", "arguments": {"path": "write.txt", "content": "ok"}},
                }
            ]
        )
    )
    deps = GraphDependencies(
        database=database,
        roles=tool_executor.roles,
        models=models,
        tool_executor=tool_executor,
        observability=Observability(settings),
    )
    async with database.session() as session:
        run_record = await Repository(session).create_run(
            "Write a file.",
            model_provider="scripted_loop",
            workspace=str(tmp_path),
        )

    with pytest.raises(ApprovalRequired):
        await _run_native_loop(
            deps,
            {
                "run_id": run_record.id,
                "thread_id": run_record.thread_id,
                "task": "Write a file.",
                "mode": RunMode.GENERAL.value,
                "model_provider": "scripted_loop",
                "workspace": str(tmp_path),
            },
            role=RoleName.CODER.value,
            node_id="coder",
            contract_id=WORKER_AGENT_OUTPUT_CONTRACT,
            prompt="Write through governed tools.",
            context={},
        )

    async with database.session() as session:
        repo = Repository(session)
        approvals = (
            await session.execute(
                select(ApprovalRecord).where(
                    ApprovalRecord.run_id == run_record.id,
                    ApprovalRecord.status == ApprovalStatus.PENDING.value,
                )
            )
        ).scalars().all()
        artifacts = await repo.list_artifacts(run_record.id)
    assert len(approvals) == 1
    assert any(artifact.kind == "native_loop_trace" and artifact.content["status"] == "waiting_approval" for artifact in artifacts)


async def test_native_worker_loop_retries_invalid_final_payload(
    settings,
    database,
    tool_executor,
    tmp_path: pathlib.Path,
) -> None:
    models = ModelProviderRegistry()
    models.register(
        ScriptedLoopProvider(
            [
                {
                    "action": "finish",
                    "summary": "Return invalid payload first.",
                    "payload": {"summary": "missing role"},
                },
                {
                    "action": "finish",
                    "summary": "Return valid payload after contract feedback.",
                    "payload": {
                        "role": "data_analyst",
                        "summary": "Valid final payload.",
                        "tool_results": [],
                        "risks": [],
                    },
                },
            ]
        )
    )
    deps = GraphDependencies(
        database=database,
        roles=tool_executor.roles,
        models=models,
        tool_executor=tool_executor,
        observability=Observability(settings),
    )

    result = await _run_native_loop(
        deps,
        {
            "run_id": "run-invalid-final",
            "thread_id": "thread-invalid-final",
            "task": "Analyze.",
            "mode": RunMode.GENERAL.value,
            "model_provider": "scripted_loop",
            "workspace": str(tmp_path),
        },
        role=RoleName.DATA_ANALYST.value,
        node_id="data_analyst",
        contract_id=WORKER_AGENT_OUTPUT_CONTRACT,
        prompt="Return a valid worker output.",
        context={},
    )

    assert result.payload["summary"] == "Valid final payload."
    assert "does not match contract" in result.trace[0]["error"]


async def test_native_worker_loop_rejects_duplicate_tool_call_without_reexecution(
    settings,
    database,
    tool_executor,
    tmp_path: pathlib.Path,
) -> None:
    (tmp_path / "README.md").write_text("# Demo\n", encoding="utf-8")
    duplicate_call = {"name": "native.fs_list", "arguments": {"glob": "*.md"}}
    models = ModelProviderRegistry()
    models.register(
        ScriptedLoopProvider(
            [
                {
                    "action": "tool_call",
                    "summary": "Search markdown files.",
                    "tool_call": duplicate_call,
                },
                {
                    "action": "tool_call",
                    "summary": "Repeat the same search.",
                    "tool_call": duplicate_call,
                },
                {
                    "action": "finish",
                    "summary": "Finish after duplicate feedback.",
                    "payload": {
                        "role": "data_analyst",
                        "summary": "Used the first search result.",
                        "tool_results": [],
                        "risks": [],
                    },
                },
            ]
        )
    )
    deps = GraphDependencies(
        database=database,
        roles=tool_executor.roles,
        models=models,
        tool_executor=tool_executor,
        observability=Observability(settings),
    )

    result = await _run_native_loop(
        deps,
        {
            "run_id": "run-duplicate-tool",
            "thread_id": "thread-duplicate-tool",
            "task": "Analyze.",
            "mode": RunMode.GENERAL.value,
            "model_provider": "scripted_loop",
            "workspace": str(tmp_path),
        },
        role=RoleName.DATA_ANALYST.value,
        node_id="data_analyst",
        contract_id=WORKER_AGENT_OUTPUT_CONTRACT,
        prompt="Return a valid worker output.",
        context={},
    )

    assert result.payload["summary"] == "Used the first search result."
    assert len(result.tool_results) == 1
    assert result.tool_results[0].tool_name == "native.fs_list"
    assert "duplicate tool call" in result.trace[1]["error"]


async def test_native_worker_loop_includes_tool_catalog(
    settings,
    database,
    tool_executor,
    tmp_path: pathlib.Path,
) -> None:
    provider = ScriptedLoopProvider(
        [
            {
                "action": "finish",
                "summary": "Finish with catalog context.",
                "payload": {
                    "role": "data_analyst",
                    "summary": "Catalog was available.",
                    "tool_results": [],
                    "risks": [],
                },
            }
        ]
    )
    models = ModelProviderRegistry()
    models.register(provider)
    deps = GraphDependencies(
        database=database,
        roles=tool_executor.roles,
        models=models,
        tool_executor=tool_executor,
        observability=Observability(settings),
    )

    result = await _run_native_loop(
        deps,
        {
            "run_id": "run-tool-catalog",
            "thread_id": "thread-tool-catalog",
            "task": "Analyze.",
            "mode": RunMode.GENERAL.value,
            "model_provider": "scripted_loop",
            "workspace": str(tmp_path),
        },
        role=RoleName.DATA_ANALYST.value,
        node_id="data_analyst",
        contract_id=WORKER_AGENT_OUTPUT_CONTRACT,
        prompt="Return a valid worker output.",
        context={},
    )

    assert result.payload["summary"] == "Catalog was available."
    catalog = {tool["name"]: tool for tool in provider.requests[0].context["tool_catalog"]}
    assert catalog["native.fs_list"]["examples"][0]["glob"] == "*.py"
    assert catalog["native.fs_search"]["input_schema"]["required"] == ["pattern"]
    assert "native.fs_write" not in catalog


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
    assert "Verification: passed" in second.final_answer
    async with database.session() as session:
        artifacts = await Repository(session).list_artifacts(first.id)
    assert len([artifact for artifact in artifacts if artifact.kind == "supervisor_decision"]) == 1
    assert len([artifact for artifact in artifacts if artifact.kind == "run_report"]) >= 1


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
    assert any(item["message_type"] == "run_report" for item in context)
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
    assert "Plan:" in result.final_answer
    assert "- data_analyst:" in result.final_answer
    assert audit == []


async def test_streaming_provider_emits_model_stream_events(service, tmp_path: pathlib.Path) -> None:
    service.models.register(StreamingFakeProvider())
    async with service.database.session() as session:
        run_record = await Repository(session).create_run(
            "Stream a concise answer",
            model_provider="streaming_fake",
            workspace=str(tmp_path),
        )
    deps = GraphDependencies(
        database=service.database,
        roles=service.roles,
        models=service.models,
        tool_executor=service.tool_executor,
        observability=service.observability,
    )

    await _invoke_model(
        deps,
        {
            "run_id": run_record.id,
            "thread_id": run_record.thread_id,
            "task": "Stream a concise answer",
            "mode": RunMode.GENERAL.value,
            "model_provider": "streaming_fake",
            "workspace": str(tmp_path),
        },
        ResolvedModelProvider(
            provider=service.models.get("streaming_fake"),
            provider_type="streaming_fake",
        ),
        ModelRequest(role=RoleName.CODER.value, prompt="Stream a concise answer"),
    )
    events = await service.list_event_responses(run_record.id, limit=500)
    event_types = [event.event_type for event in events]
    deltas = [
        event.payload["delta"]
        for event in events
        if event.event_type == EventType.MODEL_TOKEN_DELTA.value
    ]

    assert EventType.MODEL_STREAM_STARTED.value in event_types
    assert EventType.MODEL_STREAM_COMPLETED.value in event_types
    assert "".join(deltas) == "streamed agent output"
    assert any(
        event.event_type == EventType.MODEL_INVOKED.value
        and event.payload["provider"] == "streaming_fake"
        for event in events
    )


async def test_structured_model_call_retries_invalid_json_once(service, tmp_path: pathlib.Path) -> None:
    provider = StructuredRetryFakeProvider()
    service.models.register(provider)
    async with service.database.session() as session:
        run_record = await Repository(session).create_run(
            "Plan a coding task",
            model_provider="structured_retry_fake",
            workspace=str(tmp_path),
        )
    deps = GraphDependencies(
        database=service.database,
        roles=service.roles,
        models=service.models,
        tool_executor=service.tool_executor,
        observability=service.observability,
    )

    decision = await _invoke_structured(
        deps,
        {
            "run_id": run_record.id,
            "thread_id": run_record.thread_id,
            "task": "Fix the ledger project",
            "mode": RunMode.CODING.value,
            "model_provider": "structured_retry_fake",
            "workspace": str(tmp_path),
        },
        ResolvedModelProvider(
            provider=provider,
            provider_type="structured_retry_fake",
        ),
        SupervisorDecision,
        ModelRequest(
            role=RoleName.SUPERVISOR.value,
            prompt="Create a strict executable plan.",
            context={"task": "Fix the ledger project", "mode": RunMode.CODING.value},
            response_schema=SupervisorDecision,
        ),
    )
    events = await service.list_event_responses(run_record.id, limit=500)
    model_events = [event for event in events if event.event_type == EventType.MODEL_INVOKED.value]

    assert decision.selected_roles == [RoleName.CODER.value]
    assert provider.calls == 2
    assert "previous structured response was rejected" in provider.prompts[1]
    assert [event.payload["ok"] for event in model_events] == [False, True]


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
