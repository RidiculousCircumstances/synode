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
- Runtime model routing is DB-backed through model profiles. Agent roles and
  agent graphs are DB-backed after builtin YAML seed, and runs store immutable
  graph/profile snapshots.
- Realtime UI transport is SSE first. WebSocket is deferred.
- Public agent output streams over SSE by default for providers that explicitly
  support streaming. Structured JSON model calls remain non-streaming.
- Langfuse self-hosting is optional and enabled through an explicit Compose
  overlay plus `.env.observability`.
- Threads are the user-facing work unit. Runs are immutable execution attempts
  inside a thread; continuing a conversation creates a new run in the same
  thread after the previous run reaches a terminal state.
- Explicit run stops and approval rejections cancel the affected run, reject
  pending approvals, emit observable cancellation state, and unblock the thread
  for a new run.

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
- Next.js operator UI is implemented in `web/` with threads, run detail tabs,
  approvals, full-size artifacts, coding diff/tests, timeline, graph,
  observability, and settings screens. `/chat` redirects to `/threads`.
- Thread persistence and API endpoints are implemented: create/list/detail,
  rename, archive, messages, and follow-up run creation.
- Threads-first work was verified with `uv run pytest`, `uv run ruff check .`,
  `uv run mypy`, `python3 tools/guardrails.py`, `npm run lint`,
  `npm run build`, `npm run test:e2e`, `uv run synode db upgrade`,
  `docker compose up -d --build api ui`, HTTP `/health`, HTTP `/threads`, and
  `make docker-smoke`.
- UI Docker image and Compose `ui` service are implemented. UI is served on
  `http://127.0.0.1:3000` by default.
- UI runtime config defaults to `apiBaseUrl=auto`: browser clients resolve API
  as `http(s)://<current-ui-host>:8787`, so LAN access does not call client-side
  `127.0.0.1`.
- Optional Langfuse tracing is implemented through `synode.observability` and
  instruments runs, graph nodes, model calls, and tool calls.
- `docker-compose.observability.yaml` adds Langfuse web/worker plus separate
  Postgres, ClickHouse, Redis, and MinIO services.
- DB-backed configuration is implemented for encrypted secrets, model profiles,
  agent roles, and agent graphs. Run creation resolves a graph/profile snapshot;
  general mode executes selected workers in graph topological order; coding
  mode uses the same per-role model profile resolver.
- OpenAI-compatible model profiles are supported for local vLLM, LM Studio, and
  llama.cpp-style `/v1/chat/completions` endpoints.
- Verification passed for this configuration work: `uv run ruff check src tests`,
  `uv run pytest`, `npm run lint`, `npm run build`, and `npm run test:e2e`.
- Run cancellation is implemented end-to-end: tracked background run tasks,
  `POST /runs/{run_id}/stop`, terminal `cancelled` status, `run_cancelled`
  events, UI stop controls, and approval rejection that cancels the run instead
  of leaving the thread stuck in `waiting_approval`.
- Growing API lists use DB-level pagination parameters (`limit`/`offset`, plus
  event cursors where appropriate) through repository queries.
- Verification passed for cancellation/pagination work: `uv run ruff check src
  tests`, `uv run pytest`, `uv run mypy`, `python3 tools/guardrails.py`,
  `npm run lint`, `npm run build`, `npm run test:e2e`, `uv run synode db
  upgrade`, and `git diff --check`.
- Optional Docker sandbox backend is implemented for shell and Python
  execution. It uses one short-lived container per command, Docker Engine unix
  socket access, bind-mounted workspace, default `network=none`, read-only root
  filesystem, dropped capabilities, `no-new-privileges`, PID/CPU/RAM/file-size
  limits, captured stdout/stderr, and cleanup after completion.
- Docker sandbox deployment support is documented through `Dockerfile.sandbox`,
  `docker-compose.sandbox.yaml`, and `make docker-sandbox-build`.
- Verification passed for Docker sandbox work: `uv run pytest`,
  `uv run ruff check .`, `uv run mypy`, `docker compose config`,
  `docker compose -f docker-compose.yaml -f docker-compose.sandbox.yaml config`,
  `make docker-sandbox-build`, and a real `SandboxRunner` Docker smoke command.
- Synode Fabricator is implemented as a local developer CLI/docs workflow under
  `src/synode/fabricator` and `docs/fabricator`, with Synode-specific experts,
  routing profiles, prompts, stance packs, templates, smoke workflow, and tests.
- Verification passed for Fabricator work: `uv run synode fabricator validate`,
  `uv run synode fabricator smoke`, and `uv run pytest tests/test_fabricator.py`.
- Operator UI now treats model profiles as Settings-managed provider presets
  with contextual creation links from composers, and treats agent graphs as
  Workflows-managed execution presets. `/workflows` is the primary navigation
  route; `/agents` remains available.
- Verification passed for workflow/profile UX work: `npm --prefix web run
  lint`, `npm --prefix web run build`, `npm --prefix web run test:e2e`,
  Docker UI rebuild, HTTP `/workflows` 200, API `/health`, and UI
  `/api/health`.
- Approved `native.fs_write` and `native.patch_apply` mutations now execute
  through the configured sandbox runner instead of writing directly in the API
  process. Tool audit output includes sandbox backend diagnostics for these
  mutations.
- Worker daemon mode honors `SYNODE_WORKER_CONCURRENCY` through logical worker
  slots while preserving single-run `worker once` behavior.
- Workflows UI now uses compact top tabs and table views for graph presets and
  role catalog, with `/agents` kept as a compatible route.
- Run execution dispatch now uses a Procrastinate-backed queue adapter. API
  requests enqueue `run_id` jobs after marking runs queued, workers exact-claim
  the dispatched run, and `synode db upgrade` applies the queue schema.
- Agent graph runtime bindings are implemented so worker roles can execute
  through `native_langgraph` or an optional external OpenHands backend while
  Synode keeps run state, approvals, audit, artifacts, and reviewer authority.
- AgentGraph v2 is implemented with stable `nodes`, `node_edges`,
  `node_runtime_bindings`, and `node_contracts`. Role-level graph fields are
  not part of the API/runtime contract.
- Runtime node execution is recorded in `runtime_node_states` with node id,
  role, backend id, contract id, approval/external state, and terminal node
  status. OpenHands worker execution now flows through the same node execution
  envelope used by future backends.
- Synode MCP proxy is implemented: MCP servers are DB-backed runtime config,
  discovery populates `mcp.<server>.<tool>` registry entries, native nodes use
  the in-process tool gateway, and external runtimes receive scoped HTTP MCP
  proxy sessions instead of raw MCP server configs.
- AgentGraph node backend selection now applies to control and worker nodes.
  Backend/contract compatibility is validated through backend capabilities, and
  external supervisor/reviewer payloads must validate against their Synode node
  contracts before graph execution continues.

### Now:
- MVP backend and operator UI include DB-backed runtime configuration screens
  for model profiles, agent roles, and agent graphs.
- Ollama runs as a system service and serves `qwen2.5-coder:7b` on
  `127.0.0.1:11434`.
- Threads can be continued after stopping a run or rejecting an approval,
  because both paths now produce terminal `cancelled` runs.
- Thread chat now receives prior conversation context, auto-resumes approved
  runs from UI approval actions, and renders compact live status in the chat.
- Default sandbox remains `process`; Docker sandbox is opt-in with an explicit
  local operator overlay that mounts `/var/run/docker.sock`.
- Runtime diagnostics expose worker concurrency, queue backend/status, and
  whether `SYNODE_SECRETS_KEY` is configured.
- OpenHands remains disabled by default and external to Compose. Workflows that
  bind a worker node to OpenHands require `SYNODE_OPENHANDS_ENABLED=true` and an
  OpenHands base URL; otherwise run creation fails explicitly. The default HTTP
  mode targets local OpenHands Agent Server, with hosted Cloud V1 available only
  by explicit `SYNODE_OPENHANDS_API_MODE`.
- Fabricator is available through `synode fabricator ...` and remains advisory:
  it creates planning/review artifacts but does not commit, push, or bypass
  Synode runtime tool policy.
- Workflow creation is surfaced from the run/thread composers as a navigation
  action, not embedded in the task composer itself.
- Workflows UI edits graph presets as compact tables and emits only AgentGraph
  v2 node backend/contract fields.
- Settings UI manages MCP servers through a compact table and creation modal.
- Workflows UI allows backend selection for supervisor, reviewer, and worker
  nodes while keeping fixed control-node contracts read-only.

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
- Fabricator commands: `uv run synode fabricator validate`,
  `uv run synode fabricator smoke`, `make fabricator-validate`, and
  `make fabricator-smoke`.
- Observability commands: copy `.env.observability.example` to
  `.env.observability`, then `make docker-observability-up`.
