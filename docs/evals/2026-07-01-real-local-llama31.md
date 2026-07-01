# Real local Synode eval - 2026-07-01

## Scope

This eval exercised the local Synode stack end to end with a real non-Qwen
Ollama model:

- model: `llama3.1:8b`;
- API/UI/worker/Postgres/SearxNG via Docker Compose;
- queue: Procrastinate;
- sandbox: Docker backend with `network=none`;
- graph: five nodes (`supervisor`, `data_analyst`, `coder`, `web_researcher`,
  `reviewer`), all bound to `native_langgraph`;
- workspace: ignored local fixture at
  `var/workspaces/synode-real-eval`;
- API client: ignored local helper at
  `var/evals/2026-07-01-real-synode/eval_client.py`.

The fixture repository is a tiny Python ledger project with two intentionally
failing tests around refund handling. It remains reset to the failing baseline
after eval runs so future runs start from the same state.

## Scenarios

| Scenario | Result | Notes |
| --- | --- | --- |
| Model profile smoke | Passed | Ollama health, structured output, and streaming worked with `llama3.1:8b`. |
| `plan_only` | Passed | Supervisor produced a plan without mutating the workspace. |
| `plan_review` | Passed | Run entered `waiting_operator`, accepted operator approval, and completed. |
| Rejected approval | Passed | Rejected `native.fs_write` approval cancelled the run and left the thread usable. |
| Coding fix | Partially passed | The run applied an approval-gated patch through Docker sandbox and ended as managed `failed_verification`, not a system crash. |

Final coding retry run:

- run id: `7ecc03e9-f32d-4a0f-b792-bab49bb73644`;
- final status: `failed_verification`;
- approvals observed: `1`;
- model behavior: first patch was semantically correct, but the model later
  produced an invalid repair target and an unsafe verification command;
- system behavior: unsafe command was blocked and the run ended in an explicit
  failed-verification state.

## Bugs fixed during eval

- Approval-required tool calls no longer let the graph continue past the
  approval pause and overwrite cancelled/completed state.
- Approval rejection now cancels the affected run deterministically.
- `native.patch_apply` is atomic per target file and supports multiple patches
  to the same file without checksum drift.
- Patch proposals now get deterministic old-text normalization for common
  indentation mistakes.
- Real coding runs now capture pre-patch verification evidence.
- Failed real-model verification can take one repair pass, then falls back to
  reviewer with explicit `failed_verification` if repair output is invalid.
- Patch proposal validation now rejects unsafe verification commands before
  patch application.
- Ollama/OpenAI-compatible provider errors now include timeout/status details
  instead of empty messages.
- SQLAlchemy engines use `pool_pre_ping` to recover from stale DB connections.
- `SYNODE_MODEL_TIMEOUT_SECONDS` is validated as a positive startup setting.

## Residual risks

- `llama3.1:8b` is adequate for planning and simple operator flows, but not
  reliable enough as the native coding backend for unattended code repair.
- The current native coding contract is now safer, but stronger coder backends
  such as OpenHands, Codex, or Claude Code are still expected to outperform it.
- Runtime diagnostics still show historical worker heartbeat rows, which is
  noisy for operators even though current queue/running/stale counts are
  correct.

## Verification commands

- `ollama pull llama3.1:8b`
- `make docker-sandbox-build`
- `docker compose -f docker-compose.yaml -f docker-compose.sandbox.yaml up -d --build`
- `curl -fsS http://127.0.0.1:8787/health`
- `curl -fsS http://127.0.0.1:8787/runtime/status`
- `docker run --rm --network none -v /home/rd/proj/synode/var/workspaces/synode-real-eval:/workspace -w /workspace synode-sandbox:local python -m pytest -q`
- `uv run pytest tests/test_orchestration.py tests/test_tools.py tests/test_model_provider.py tests/test_worker.py tests/test_api.py -q`
