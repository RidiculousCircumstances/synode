# Verification Policy

Use risk-based verification.

## Profiles

- `analysis`: no code changes; no tests required.
- `docs`: `git diff --check`.
- `small-code`: focused tests plus `make lint`.
- `critical-code`: focused tests, `make test`, `make lint`,
  `make typecheck`, `make guardrails`, `make ui-lint`, `make ui-build`, and
  `make ui-test` when UI code changes.

## Local Commands

```bash
make test
make lint
make typecheck
make guardrails
make ui-lint
make ui-build
make ui-test
make smoke
make docker-smoke
```

`make smoke` is a deterministic fake-provider smoke and must not require a real
LLM. Real-provider validation should use `uv run synode models health` and an
explicit `--model-provider ollama` run.

`make docker-smoke` validates the Docker Compose deployment path. It expects:

- Docker Compose stack is running with `docker compose up -d --build`.
- Ollama is running outside Docker and reachable from the API container through
  host networking at `http://127.0.0.1:11434`.
- `qwen2.5-coder:7b` is installed in Ollama.

Docker config checks:

```bash
docker compose config
docker compose --env-file .env.observability.example \
  -f docker-compose.yaml \
  -f docker-compose.observability.yaml \
  --profile observability \
  config
```

UI checks:

- `make ui-lint` validates TypeScript/React lint rules.
- `make ui-build` validates the production Next.js standalone build.
- `make ui-test` runs Playwright desktop and mobile layout smoke for chat,
  runs, run detail tabs, agent graph, timeline, artifacts, diff/tests,
  observability, settings, and browser API auto-resolution.
