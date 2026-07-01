from __future__ import annotations

SAFE_COMMANDS = {"rg", "ls", "pwd", "git", "pytest", "python", "python3", "uv"}
SAFE_GIT_SUBCOMMANDS = {"status", "diff", "show", "log"}
SAFE_UV_SUBCOMMANDS = {"run"}
SAFE_PYTHON_MODULES = {"pytest"}


def is_safe_command(argv: list[str]) -> bool:
    if not argv:
        return False
    command = str(argv[0])
    if command not in SAFE_COMMANDS:
        return False
    if command == "git" and len(argv) > 1 and str(argv[1]) not in SAFE_GIT_SUBCOMMANDS:
        return False
    if command == "uv" and len(argv) > 1 and str(argv[1]) not in SAFE_UV_SUBCOMMANDS:
        return False
    if command in {"python", "python3"} and len(argv) > 2 and argv[1] == "-m" and argv[2] not in SAFE_PYTHON_MODULES:
        return False
    return True
