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
```

`make smoke` is a deterministic fake-provider smoke and must not require a real
LLM. Real-provider validation should use `uv run synode models health` and an
explicit `--model-provider ollama` run.
