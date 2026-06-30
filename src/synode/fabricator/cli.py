from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from synode.fabricator import council, fabricator
from synode.fabricator.common import FabricatorError


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Synode Fabricator workflow helper")
    sub = parser.add_subparsers(dest="command", required=True)

    def add_run_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        item = sub.add_parser(name, help=help_text)
        item.add_argument("--run-id", required=True)
        item.add_argument("--runs-dir", type=Path, default=fabricator.DEFAULT_RUNS_DIR)
        return item

    sub.add_parser("validate", help="Validate Fabricator registry, routing, prompts, and templates")

    start = sub.add_parser("start", help="Create a Fabricator run workspace")
    start.add_argument("--mode", choices=sorted(fabricator.VALID_MODES), default="plan-patch")
    start.add_argument("--goal", required=True)
    start.add_argument("--paths", nargs="*", default=[])
    start.add_argument("--profile")
    start.add_argument("--experts", nargs="+")
    start.add_argument("--expert-override-reason")
    start.add_argument("--run-id")
    start.add_argument("--runs-dir", type=Path, default=fabricator.DEFAULT_RUNS_DIR)

    add_run_parser("next", "Show the next allowed Fabricator step")
    add_run_parser("advance", "Run the next safe Fabricator runner step")
    add_run_parser("summary", "Write and show a Fabricator run summary")
    add_run_parser("render-prompts", "Render expert prompts for an existing run")

    challenge = add_run_parser("render-challenge-prompts", "Render challenge prompts for selected experts")
    challenge.add_argument("--experts", nargs="+", required=True)
    challenge.add_argument("--reason", required=True)

    add_run_parser("dispatch-experts", "Write Codex subagent dispatch plan for experts")
    validate_responses = add_run_parser("validate-responses", "Validate structured response sidecars")
    validate_responses.add_argument("--phase", choices=sorted(council.VALID_RESPONSE_PHASES), required=True)
    add_run_parser("synthesize", "Build Arbiter synthesis from expert responses")
    add_run_parser("dispatch-challenge", "Write Codex subagent dispatch plan for challenge")
    add_run_parser("trace-decision", "Build and validate Arbiter decision traceability")
    add_run_parser("mark-decision-ready", "Mark Arbiter decision and handoff ready")
    add_run_parser("mark-implementation-ready", "Mark implementation ready for review")
    add_run_parser("dispatch-review", "Write Codex subagent dispatch plan for review")
    add_run_parser("agent-timeouts", "Report overdue Fabricator subagents")
    add_run_parser("status", "Read Fabricator run status")

    for name in ("agent-started", "agent-completed", "agent-failed"):
        item = add_run_parser(name, f"Record Fabricator {name.replace('-', ' ')} event")
        item.add_argument("--phase", choices=sorted(council.VALID_RESPONSE_PHASES), required=True)
        item.add_argument("--expert", required=True)
        if name == "agent-started":
            item.add_argument("--agent-id")
        if name == "agent-failed":
            item.add_argument("--reason", required=True)

    finalize = sub.add_parser("finalize", help="Finalize a Fabricator run without committing")
    finalize.add_argument("--run-id", required=True)
    finalize.add_argument("--result", choices=sorted(fabricator.VALID_RESULTS), required=True)
    finalize.add_argument("--runs-dir", type=Path, default=fabricator.DEFAULT_RUNS_DIR)

    smoke = sub.add_parser("smoke", help="Run a local Fabricator smoke under /tmp")
    smoke.add_argument("--runs-dir", type=Path, default=Path("/tmp/synode-fabricator-smoke-runs"))

    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        payload = run_command(args)
    except FabricatorError as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, indent=2), file=sys.stderr)
        return 2
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def run_command(args: argparse.Namespace) -> dict[str, object]:
    if args.command == "validate":
        return fabricator.validate_all()
    if args.command == "start":
        return fabricator.start_run(
            mode=args.mode,
            goal=args.goal,
            paths=args.paths,
            profile_id=args.profile,
            run_id=args.run_id,
            runs_dir=args.runs_dir,
            expert_override_ids=args.experts,
            expert_override_reason=args.expert_override_reason,
        )
    run_id = getattr(args, "run_id", "")
    runs_dir = getattr(args, "runs_dir", fabricator.DEFAULT_RUNS_DIR)
    if args.command == "next":
        return fabricator.next_step(run_id, runs_dir)
    if args.command == "advance":
        return fabricator.advance(run_id, runs_dir)
    if args.command == "summary":
        return fabricator.summarize(run_id, runs_dir)
    if args.command == "render-prompts":
        return fabricator.render_prompts(run_id, runs_dir)
    if args.command == "render-challenge-prompts":
        return fabricator.render_challenge_prompts(
            run_id=run_id,
            runs_dir=runs_dir,
            expert_ids=args.experts,
            reason=args.reason,
        )
    if args.command == "dispatch-experts":
        return fabricator.dispatch_experts(run_id, runs_dir)
    if args.command == "validate-responses":
        return fabricator.validate_responses(run_id, runs_dir, args.phase)
    if args.command == "synthesize":
        return fabricator.synthesize(run_id, runs_dir)
    if args.command == "dispatch-challenge":
        return fabricator.dispatch_challenge(run_id, runs_dir)
    if args.command == "trace-decision":
        return fabricator.trace_decision(run_id, runs_dir)
    if args.command == "mark-decision-ready":
        return fabricator.mark_decision_ready(run_id, runs_dir)
    if args.command == "mark-implementation-ready":
        return fabricator.mark_implementation_ready(run_id, runs_dir)
    if args.command == "dispatch-review":
        return fabricator.dispatch_review(run_id, runs_dir)
    if args.command == "agent-started":
        return fabricator.agent_started(run_id, runs_dir, args.phase, args.expert, args.agent_id)
    if args.command == "agent-completed":
        return fabricator.agent_completed(run_id, runs_dir, args.phase, args.expert)
    if args.command == "agent-failed":
        return fabricator.agent_failed(run_id, runs_dir, args.phase, args.expert, args.reason)
    if args.command == "agent-timeouts":
        return fabricator.agent_timeouts(run_id, runs_dir)
    if args.command == "status":
        return fabricator.status_run(run_id, runs_dir)
    if args.command == "finalize":
        return fabricator.finalize_run(run_id, runs_dir, args.result)
    if args.command == "smoke":
        from synode.fabricator import smoke

        return smoke.smoke(args.runs_dir)
    raise FabricatorError(f"unknown command: {args.command}")
