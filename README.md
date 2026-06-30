# Synode

Synode is a trusted local/operator LangGraph multi-agent MVP. It provides a
supervisor, specialized worker agents, native tools, MCP tools, approvals,
audit logging, Postgres persistence, CLI, and FastAPI. It intentionally does
not include app-level authentication or RBAC and must not be exposed directly
to the public internet.

The MVP is model-agnostic at the orchestration boundary and ships with an
Ollama provider configured for `qwen2.5-coder:7b`. The deterministic `fake`
provider is kept for tests and explicit local diagnostics only.

Model routing is configured through database-backed model profiles. Supported
profile types are `ollama`, `openai_compatible`, and `fake`. Agent roles and
agent graphs are also database-backed after startup seed from the builtin YAML
roles. A run stores a snapshot of the selected graph plus the selected default
model profile and per-role profile overrides, so later role/graph edits do not
rewrite historical runs.

## Quick Start

### Docker Compose

Ollama runs outside Docker on the host. The compose stack starts Synode API,
Synode worker, Synode UI, Postgres, SearxNG, and a one-shot migration job. On
Linux, the API and worker containers use host networking so they can reach the
host-local Ollama service at `127.0.0.1:11434` without exposing Ollama on all
interfaces.

```bash
systemctl status ollama --no-pager
docker compose up -d --build
curl http://127.0.0.1:8787/models/health
curl http://127.0.0.1:8787/runtime/status
make docker-smoke
```

The API is available at `http://127.0.0.1:8787`. The operator UI is available
at `http://127.0.0.1:3000`. When the UI is opened from another device on the
local network, it resolves the API host from the browser URL. For example,
`http://192.168.1.50:3000` will call `http://192.168.1.50:8787`. Compose uses
`http://127.0.0.1:11434` for the external Ollama endpoint from the API and
worker containers' host network.

Optional port/model/runtime overrides can be copied from `.env.docker.example`
into `.env`. Risky native mutations and command execution require an explicit
sandbox backend. The default local backend is `SYNODE_SANDBOX_BACKEND=process`,
which enforces workspace, timeout, output, CPU, RAM, and file-size limits. Set
`SYNODE_SANDBOX_BACKEND=none` only for diagnostics; approved write tools will
then fail closed.

If you store API keys in Synode DB secrets, set `SYNODE_SECRETS_KEY` before
starting the API. Without it, secret creation and secret-backed profiles fail
explicitly. Ollama-only local use does not require this key.

### Observability

Langfuse self-hosting is available as an explicit overlay. It is intentionally
separate from the default compose stack because Langfuse v3 includes web,
worker, Postgres, ClickHouse, Redis, and MinIO services.

```bash
cp .env.observability.example .env.observability
# Replace local example secrets before sharing the host.
make docker-observability-up
```

Langfuse UI is available at `http://127.0.0.1:3001`. When the overlay is used,
Synode API starts with Langfuse enabled and fails explicitly if required
Langfuse keys are missing.

### Local Python

```bash
uv sync --extra dev
docker compose up -d postgres searxng
uv run synode db upgrade
ollama pull qwen2.5-coder:7b
uv run synode models health
uv run synode run "Analyze sample data and summarize findings" --model-provider ollama
uv run synode serve --host 127.0.0.1 --port 8787
uv run synode worker run
```

UI development:

```bash
npm install --prefix web
make ui-dev
make ui-lint
make ui-build
make ui-test
```

The browser UI is a Next.js operator console with thread-based conversations,
run-detail tabs, agent graph monitoring, timeline, approvals, artifacts,
diff/tests, settings, and observability views. Threads are the user-facing
workspace; runs are immutable execution attempts inside a thread.

API requests enqueue runs and return quickly. A worker process claims queued
runs from Postgres, heartbeats while executing, and requeues stale running runs
after worker heartbeat expiry. Active runs can be stopped from the API/UI. A
queued or waiting run becomes terminal `cancelled`; a worker-owned run first
enters `cancelling` so the worker can cancel the in-flight graph. Rejecting an
approval also cancels that run, because the rejected mutation must not resume
implicitly. Growing list endpoints accept `limit` and `offset`; repository
queries apply pagination in the database.

Deterministic test smoke without a real model:

```bash
make smoke
```

Coding workflow:

```bash
uv run synode run "Inspect this repo and propose a tiny README wording change" \
  --mode coding \
  --workspace . \
  --model-provider ollama
```

File mutations require approval. Use `synode approve` and `synode resume` to
continue a waiting run, or reject/stop it to cancel the run and continue the
thread with a new request.

Operational commands:

```bash
make runtime-status
make cleanup
make backup
make restore BACKUP=var/backups/synode-YYYYmmdd-HHMMSS.sql
```

Retention cleanup prunes old run events, token deltas, tool audit records,
artifacts, and archived threads according to `SYNODE_*_RETENTION_DAYS`
settings. See `docs/production.md` for trusted-LAN and backup guidance.

Configuration UI:

- Settings: model health, model profiles, and encrypted secret records.
- Agents: DB-backed role catalog and simple graph configuration.
- New thread / follow-up composer: selects agent graph and model profile.

## Project Rules

Read `agents.md` before changing the repository.
