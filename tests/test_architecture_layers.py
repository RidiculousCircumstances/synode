from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "synode"


def test_domain_does_not_import_outer_layers() -> None:
    forbidden = (
        "synode.api",
        "synode.cli",
        "synode.application",
        "synode.infrastructure",
        "synode.persistence",
        "synode.runtime",
        "synode.tools",
        "synode.models",
    )
    assert_no_imports(SRC / "domain", forbidden)


def test_application_does_not_import_api_or_infrastructure_adapters() -> None:
    forbidden = (
        "synode.api",
        "synode.cli",
        "synode.infrastructure",
        "synode.persistence",
    )
    assert_no_imports(SRC / "application", forbidden)


def assert_no_imports(path: Path, forbidden: tuple[str, ...]) -> None:
    violations: list[str] = []
    for file_path in sorted(path.rglob("*.py")):
        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=str(file_path))
        for node in ast.walk(tree):
            module = None
            if isinstance(node, ast.ImportFrom):
                module = node.module
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    imported = alias.name
                    if imported.startswith(forbidden):
                        violations.append(f"{file_path.relative_to(ROOT)} imports {imported}")
                continue
            if module and module.startswith(forbidden):
                violations.append(f"{file_path.relative_to(ROOT)} imports {module}")
    assert violations == []

