# Continuity Ledger

## Goal (success criteria):
- Build a full MVP multi-agent application in `/home/rd/proj/synode`.
- MVP uses LangGraph, CLI + FastAPI, Postgres persistence, MCP support, native
  tools, and a model-agnostic provider layer with a deterministic fake provider.

## Constraints/Assumptions:
- No specific production LLM is selected.
- Default tool mode is read-only; mutating actions require approval.
- Postgres is the default state store.
- SearxNG is the default local web-search backend.
- Web UI is local-only and unauthenticated. Production auth, distributed queue,
  vector DB, deploy automation, and database writes are not in MVP.

## Key decisions:
- Use Python with `uv`.
- Keep model selection behind provider interfaces.
- Use role YAML files for agent registry.
- Treat MCP as a governed tool layer, not a source of authority.
- Project-wide invariant: no silent fallbacks. Missing capabilities, provider
  failures, structured-output validation errors, policy denials, unsafe tool
  calls, and unavailable MCP tools must return explicit errors or approval
  states.
- Real-provider target is Ollama with `qwen2.5-coder:7b`; fake provider is
  explicit test/diagnostic only.
- Realtime UI transport is SSE first. WebSocket is deferred.
- Langfuse self-hosting is optional and enabled through an explicit Compose
  overlay plus `.env.observability`.

## State:
### Done:
- Planning decisions captured from user.
- Repository scaffold, governance docs, architecture map, CLI, FastAPI,
  LangGraph runtime, role registry, model provider abstraction, native tools,
  MCP bridge, Postgres persistence, Alembic migration, compose dependencies,
  samples, and tests are implemented.
- Ollama provider is implemented as the default real provider with
  `qwen2.5-coder:7b`; fake provider remains explicit test/diagnostic only.
- Supervisor, reviewer, coding inspection, patch proposal, and verification
  planning use strict structured Pydantic output with explicit validation
  errors.
- Coding mode is implemented with repository inspection, structured patch
  proposal, approval-gated patch apply, focused verification commands, resume,
  reviewer pass, and failed-verification status.
- Verification passed: `uv run pytest`, `uv run ruff check .`,
  `uv run mypy`, `python3 tools/guardrails.py`, `uv run synode db upgrade`
  against compose Postgres, `make smoke`, `git diff --check`, HTTP `/health`,
  and HTTP `/models/health`.
- API is running at `http://127.0.0.1:8787` from the current working tree.
- Ollama was installed user-local under `/home/rd/.local/ollama`, and
  `qwen2.5-coder:7b` is pulled under `/home/rd/.ollama/models`.
- The temporary user systemd unit was stopped, disabled, and removed. A
  system-wide unit is prepared at `ops/systemd/ollama.service`, with installer
  script `ops/systemd/install-ollama-system-service.sh`.
- The system-wide Ollama unit is installed at `/etc/systemd/system/ollama.service`
  and reports enabled/active. Synode model health reports Ollama `ok=true`.
- Real-provider validation passed with `make smoke-ollama`.
- Docker Compose quick deployment is implemented for Synode API, Postgres, and
  SearxNG. Ollama remains outside Docker and is reached through
  API host networking at `127.0.0.1:11434`.
- Docker Compose deployment was validated with `docker compose up -d --build`
  and `make docker-smoke`.
- Structured API read models are implemented for runs, events, artifacts,
  approvals, tool audit, run metrics, and system metrics.
- SSE now emits event ids, event names, JSON data envelopes, and heartbeats.
- Next.js operator UI is implemented in `web/` with chat/run launcher, runs
  list, run detail tabs, approvals, full-size artifacts, coding diff/tests,
  timeline, graph, observability, and settings screens.
- UI Docker image and Compose `ui` service are implemented. UI is served on
  `http://127.0.0.1:3000` by default.
- UI runtime config defaults to `apiBaseUrl=auto`: browser clients resolve API
  as `http(s)://<current-ui-host>:8787`, so LAN access does not call client-side
  `127.0.0.1`.
- Optional Langfuse tracing is implemented through `synode.observability` and
  instruments runs, graph nodes, model calls, and tool calls.
- `docker-compose.observability.yaml` adds Langfuse web/worker plus separate
  Postgres, ClickHouse, Redis, and MinIO services.

### Now:
- MVP backend and operator UI implementation are ready for local verification.
- Ollama runs as a system service and serves `qwen2.5-coder:7b` on
  `127.0.0.1:11434`.

### Next:
- Tune real-model prompts against broader local workloads.
- Add production auth before exposing UI/API outside localhost.
- Add Prometheus/Grafana metrics if host-level dashboards are required.

## Open questions:
- None.

## Working set (files/ids/commands):
- `/home/rd/proj/synode`
- Compose Postgres and SearxNG services were started for verification.
- Frontend commands: `make ui-lint`, `make ui-build`, `make ui-test`,
  `make ui-dev`.
- Observability commands: copy `.env.observability.example` to
  `.env.observability`, then `make docker-observability-up`.
