PYTHON ?= python3
UV ?= uv
PYTEST ?= $(UV) run pytest

.PHONY: dev-install test lint typecheck guardrails smoke smoke-ollama db-upgrade serve docker-up docker-down docker-logs docker-smoke

dev-install:
	$(UV) sync --extra dev

db-upgrade:
	$(UV) run synode db upgrade

test:
	$(PYTEST)

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run mypy

guardrails:
	$(PYTHON) tools/guardrails.py

smoke:
	$(UV) run synode run "Analyze sample data and summarize findings" --workspace samples --model-provider fake

smoke-ollama:
	$(UV) run synode models health
	$(UV) run synode run "Analyze sample data and summarize findings" --workspace samples --model-provider ollama

serve:
	$(UV) run synode serve --host 127.0.0.1 --port 8787

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f api

docker-smoke:
	docker compose exec api synode models health
	docker compose exec api synode run "Analyze sample data and summarize findings" --workspace /app/samples --model-provider ollama
