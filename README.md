# Synode

Synode is a local-first LangGraph multi-agent MVP. It provides a supervisor,
specialized worker agents, native tools, MCP tools, approvals, audit logging,
Postgres persistence, CLI, and FastAPI.

The MVP is model-agnostic. It ships with a deterministic `fake` provider for
tests and demos. Production model providers are configured separately.

## Quick Start

```bash
uv sync --extra dev
docker compose up -d postgres searxng
uv run synode db upgrade
uv run synode run "Analyze sample data and summarize findings" --model-provider fake
uv run synode serve --host 127.0.0.1 --port 8787
```

## Project Rules

Read `agents.md` before changing the repository.

