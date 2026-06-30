# Synode

Synode is a local-first LangGraph multi-agent MVP. It provides a supervisor,
specialized worker agents, native tools, MCP tools, approvals, audit logging,
Postgres persistence, CLI, and FastAPI.

The MVP is model-agnostic at the orchestration boundary and ships with an
Ollama provider configured for `qwen2.5-coder:7b`. The deterministic `fake`
provider is kept for tests and explicit local diagnostics only.

## Quick Start

### Docker Compose

Ollama runs outside Docker on the host. The compose stack starts Synode API,
Synode UI, Postgres, and SearxNG. On Linux, the API container uses host
networking so it can reach the host-local Ollama service at
`127.0.0.1:11434` without exposing Ollama on all interfaces.

```bash
systemctl status ollama --no-pager
docker compose up -d --build
curl http://127.0.0.1:8787/models/health
make docker-smoke
```

The API is available at `http://127.0.0.1:8787`. The operator UI is available
at `http://127.0.0.1:3000`. When the UI is opened from another device on the
local network, it resolves the API host from the browser URL. For example,
`http://192.168.1.50:3000` will call `http://192.168.1.50:8787`. Compose uses
`http://127.0.0.1:11434` for the external Ollama endpoint from the API
container's host network.

Optional port/model overrides can be copied from `.env.docker.example` into
`.env`.

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
continue a waiting run.

## Project Rules

Read `agents.md` before changing the repository.
