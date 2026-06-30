# Synode

Synode is a local-first LangGraph multi-agent MVP. It provides a supervisor,
specialized worker agents, native tools, MCP tools, approvals, audit logging,
Postgres persistence, CLI, and FastAPI.

The MVP is model-agnostic at the orchestration boundary and ships with an
Ollama provider configured for `qwen2.5-coder:7b`. The deterministic `fake`
provider is kept for tests and explicit local diagnostics only.

## Quick Start

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
