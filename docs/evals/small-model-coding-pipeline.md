# Small-model coding pipeline

Synode's native coding workflow is optimized for trusted local 7-8B models by
shrinking each model call into a strict, evidence-based step.

## Runtime behavior

- Native worker execution runs as a bounded action/observation loop. Each step
  must be a structured `NativeLoopAction`: call one allowed tool, ask the
  operator, or finish with a valid node contract payload.
- Each native loop receives a per-role `tool_catalog` with descriptions,
  JSON-style input schemas, examples, and tool-specific mistakes to avoid.
- Filesystem navigation is split deliberately: `native.fs_list` lists files,
  while `native.fs_search` searches file contents with a required regex
  `pattern`. Invalid arguments are returned as tool observations instead of
  crashing the run.
- Coding inspection and patch proposal use the same native loop boundary, so
  small models can inspect, observe, and retry before returning structured
  `CodingInspection` or `PatchProposal` payloads.
- Coding runs create a `coding_context_packet` artifact before patch proposal.
- The packet repeats the task, strict editing rules, allowed verification
  commands, relevant file windows, file hashes, inspection output, and repair
  evidence when applicable.
- `PatchProposal` supports three actions:
  - `patch`: apply approval-gated file patches;
  - `no_change`: run verification without mutating the workspace;
  - `needs_operator`: stop with an operator ambiguity request.
- Verification commands must come from the Synode-generated command catalog.
  The model no longer gets a second free-form verification planning call.
- Invalid patch candidates, unsafe verification commands, and bad repair output
  become explicit failure categories and reviewer blockers, not system crashes.

## Benchmark tasks

Tracked task templates live in `src/synode/evals/coding_tasks.json`. Reports are
written under ignored `var/evals/...`. Runtime workspaces are written under
ignored `var/workspaces/evals/...`, which Docker Compose mounts into API/worker
containers as `/workspace/evals/...`.

The current suite covers:

- single-file refund accounting;
- multi-file inclusive CLI date filtering;
- config precedence;
- idempotent markdown TOC generation;
- no-change guard;
- ambiguous requirement/operator stop;
- unsafe verification contract regression.

## Commands

List benchmark tasks:

```bash
uv run synode eval coding --list-tasks
```

Materialize fixtures without calling the API:

```bash
uv run synode eval coding --dry-run --output-dir var/evals/coding-dry-run
```

Run the real API/worker/sandbox benchmark:

```bash
uv run synode eval coding --backend native_langgraph --model llama3.1:8b
uv run synode eval coding --backend native_langgraph --model qwen2.5-coder:7b
uv run synode eval coding --backend openhands --model llama3.1:8b
```

For Docker Compose, the default workspace mapping is:

```bash
--workspace-host-dir var/workspaces/evals --api-workspace-dir /workspace/evals
```

For a non-container API process, set `--api-workspace-dir` to a path that the API
process can resolve and that is included in `SYNODE_WORKSPACE_ALLOWLIST`, or pass
an empty value to send host paths directly.

The Make target defaults to `llama3.1:8b`:

```bash
make eval-coding
```

OpenHands eval mode binds only the coder node to `openhands`; supervisor and
reviewer stay native. The native-only unsafe verification contract task is
skipped in OpenHands mode because it exercises Synode `PatchProposal`
validation rather than black-box node execution.

## Acceptance

- Safety pass should be 100%: no unauthorized mutations, unsafe verification is
  blocked, and bad model output produces typed failure artifacts.
- Contract pass should be 100%: all model outputs validate or fail explicitly.
- Functional pass is tracked per model. The initial target for a useful small
  local model is at least 5 of 7 tasks.
