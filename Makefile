PYTHON ?= python3
UV ?= uv
PYTEST ?= $(UV) run pytest

.PHONY: dev-install test lint typecheck guardrails smoke smoke-ollama eval-coding fabricator-validate fabricator-smoke db-upgrade queue-upgrade serve worker runtime-status cleanup backup restore ui-dev ui-build ui-lint ui-test docker-up docker-down docker-logs docker-smoke docker-sandbox-build docker-sandbox-up docker-observability-up docker-observability-down

dev-install:
	$(UV) sync --extra dev

db-upgrade:
	$(UV) run synode db upgrade

queue-upgrade:
	$(UV) run synode queue upgrade

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

eval-coding:
	$(UV) run synode eval coding --model llama3.1:8b

fabricator-validate:
	$(UV) run synode fabricator validate

fabricator-smoke:
	$(UV) run synode fabricator smoke

serve:
	$(UV) run synode serve --host 127.0.0.1 --port 8787

worker:
	$(UV) run synode worker run

runtime-status:
	$(UV) run synode runtime status

cleanup:
	$(UV) run synode maintenance cleanup

backup:
	mkdir -p var/backups
	docker compose exec -T postgres pg_dump -U synode -d synode > var/backups/synode-$$(date +%Y%m%d-%H%M%S).sql

restore:
	test -n "$(BACKUP)"
	docker compose exec -T postgres psql -U synode -d synode < "$(BACKUP)"

ui-dev:
	npm --prefix web run dev

ui-build:
	npm --prefix web run build

ui-lint:
	npm --prefix web run lint

ui-test:
	npm --prefix web run test:e2e

docker-up:
	docker compose up -d --build

docker-down:
	docker compose down

docker-logs:
	docker compose logs -f api

docker-smoke:
	docker compose exec api synode models health
	docker compose exec api synode run "Analyze sample data and summarize findings" --workspace /app/samples --model-provider ollama

docker-sandbox-build:
	docker build -f Dockerfile.sandbox -t synode-sandbox:local .

docker-sandbox-up:
	$(MAKE) docker-sandbox-build
	docker compose -f docker-compose.yaml -f docker-compose.sandbox.yaml up -d --build

docker-observability-up:
	test -f .env.observability
	docker compose --env-file .env.observability -f docker-compose.yaml -f docker-compose.observability.yaml --profile observability up -d --build

docker-observability-down:
	docker compose --env-file .env.observability -f docker-compose.yaml -f docker-compose.observability.yaml --profile observability down
