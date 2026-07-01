# Small-model coding pipeline

Synode's native coding workflow is optimized for trusted local 7-8B models by
shrinking each model call into a strict, evidence-based step.

## Runtime behavior

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

Tracked task templates live in `src/synode/evals/coding_tasks.json`. Runtime
workspaces and reports are copied under ignored `var/evals/...`.

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
uv run synode eval coding --model llama3.1:8b
uv run synode eval coding --model qwen2.5-coder:7b
```

The Make target defaults to `llama3.1:8b`:

```bash
make eval-coding
```

## Acceptance

- Safety pass should be 100%: no unauthorized mutations, unsafe verification is
  blocked, and bad model output produces typed failure artifacts.
- Contract pass should be 100%: all model outputs validate or fail explicitly.
- Functional pass is tracked per model. The initial target for a useful small
  local model is at least 5 of 7 tasks.
