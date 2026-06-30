# Verification Policy

Use risk-based verification.

## Profiles

- `analysis`: no code changes; no tests required.
- `docs`: `git diff --check`.
- `small-code`: focused tests plus `make lint`.
- `critical-code`: focused tests, `make test`, `make lint`,
  `make typecheck`, and `make guardrails`.

## Local Commands

```bash
make test
make lint
make typecheck
make guardrails
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
