from __future__ import annotations

import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
ROLE_DIR = ROOT / "src" / "synode" / "agents"
HARDCODED_MODEL_RE = re.compile(r"\b(gpt-|claude-|gemini-|llama-|qwen-|deepseek-)", re.IGNORECASE)


def main() -> int:
    failures: list[str] = []
    for role_file in sorted(ROLE_DIR.glob("*.yaml")):
        text = role_file.read_text(encoding="utf-8")
        if "allowed_tools:" not in text:
            failures.append(f"{role_file}: missing allowed_tools")
        if HARDCODED_MODEL_RE.search(text):
            failures.append(f"{role_file}: role file appears to hardcode a model id")

    agents = ROOT / "agents.md"
    architecture = ROOT / "architecture.yml"
    if not agents.exists():
        failures.append("agents.md is missing")
    if not architecture.exists():
        failures.append("architecture.yml is missing")

    if failures:
        for failure in failures:
            print(failure, file=sys.stderr)
        return 1
    print("guardrails passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

