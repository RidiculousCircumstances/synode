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
Postgres, and SearxNG. On Linux, the API container uses host networking so it
can reach the host-local Ollama service at `127.0.0.1:11434` without exposing
Ollama on all interfaces.

```bash
systemctl status ollama --no-pager
docker compose up -d --build
curl http://127.0.0.1:8787/models/health
make docker-smoke
```

The API is available at `http://127.0.0.1:8787`. Compose uses
`http://127.0.0.1:11434` for the external Ollama endpoint from the API
container's host network.

Optional port/model overrides can be copied from `.env.docker.example` into
`.env`.

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
