PYTHON ?= python3
UV ?= uv
PYTEST ?= $(UV) run pytest

.PHONY: dev-install test lint typecheck guardrails smoke db-upgrade serve

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

serve:
	$(UV) run synode serve --host 127.0.0.1 --port 8787

