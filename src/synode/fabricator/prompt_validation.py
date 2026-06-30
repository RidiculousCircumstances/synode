from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from synode.fabricator.common import FabricatorError

REQUIRED_PROMPT_SECTIONS = (
    "Mission",
    "Non-Goals",
    "Default Biases",
    "Decision Heuristics",
    "Required Evidence",
    "Common Failure Modes",
    "Anti-Patterns",
    "Escalation Triggers",
    "Output Contract",
)

REQUIRED_STANCE_SECTIONS = (
    "Hard Preference Order",
    "Blocker Doctrine",
    "Tradeoff Defaults",
    "Critique Rubric",
    "Severity Calibration",
    "Role Blind Spots",
)


def prompt_sections(text: str) -> set[str]:
    return markdown_sections(text)


def stance_sections(text: str) -> set[str]:
    return markdown_sections(text)


def markdown_sections(text: str) -> set[str]:
    return {
        line.removeprefix("## ").strip()
        for line in text.splitlines()
        if line.startswith("## ")
    }


def validate_section_files(
    ids: set[str],
    directory: Path,
    required_sections: tuple[str, ...],
    missing_label: str,
    section_reader: Callable[[str], set[str]],
    display_path: Callable[[Path], str],
) -> None:
    missing = sorted(item_id for item_id in ids if not (directory / f"{item_id}.md").exists())
    if missing:
        raise FabricatorError(f"missing {missing_label} files: {', '.join(missing)}")
    violations: list[str] = []
    for item_id in sorted(ids):
        path = directory / f"{item_id}.md"
        sections = section_reader(path.read_text(encoding="utf-8"))
        missing_sections = [section for section in required_sections if section not in sections]
        if missing_sections:
            violations.append(f"{display_path(path)} missing sections: {', '.join(missing_sections)}")
    if violations:
        raise FabricatorError("; ".join(violations))
