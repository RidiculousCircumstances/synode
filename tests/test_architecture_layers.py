from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src" / "synode"


def test_domain_does_not_import_outer_layers() -> None:
    forbidden = (
        "synode.interfaces",
        "synode.application",
        "synode.infrastructure",
        *REMOVED_ROOT_MODULES,
    )
    assert_no_imports(SRC / "domain", forbidden)


def test_application_does_not_import_api_or_infrastructure_adapters() -> None:
    forbidden = (
        "synode.interfaces",
        "synode.infrastructure",
        *REMOVED_ROOT_MODULES,
    )
    assert_no_imports(SRC / "application", forbidden)


def test_infrastructure_does_not_import_interfaces() -> None:
    assert_no_imports(SRC / "infrastructure", ("synode.interfaces", *REMOVED_ROOT_MODULES))


def test_removed_root_modules_are_not_tracked_sources() -> None:
    missing = [module for module in REMOVED_ROOT_PATHS if (SRC / module).exists()]
    assert missing == []


REMOVED_ROOT_MODULES = (
    "synode.runtime",
    "synode.persistence",
    "synode.tools",
    "synode.models",
    "synode.schemas",
    "synode.registry",
    "synode.config",
    "synode.observability",
    "synode.security",
    "synode.api",
    "synode.cli",
)

REMOVED_ROOT_PATHS = (
    "runtime",
    "persistence",
    "tools",
    "models",
    "schemas.py",
    "registry.py",
    "config.py",
    "observability.py",
    "security.py",
    "api.py",
    "cli.py",
)


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
