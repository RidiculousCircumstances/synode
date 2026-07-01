from __future__ import annotations

import pathlib


class WorkspacePolicy:
    def __init__(self, allowlist: list[pathlib.Path]):
        self.allowlist = [path.resolve() for path in allowlist]

    def resolve_workspace(self, workspace: str | None) -> pathlib.Path:
        root = pathlib.Path(workspace or ".").expanduser().resolve()
        self._assert_allowed(root)
        if not root.exists():
            raise FileNotFoundError(f"workspace does not exist: {root}")
        if not root.is_dir():
            raise NotADirectoryError(f"workspace is not a directory: {root}")
        return root

    def resolve_path(self, workspace: str | None, path: str) -> pathlib.Path:
        root = self.resolve_workspace(workspace)
        candidate = pathlib.Path(path).expanduser()
        if not candidate.is_absolute():
            candidate = root / candidate
        resolved = candidate.resolve()
        self._assert_allowed(resolved)
        if not self._is_relative_to(resolved, root):
            raise PermissionError(f"path escapes workspace: {resolved}")
        return resolved

    def _assert_allowed(self, path: pathlib.Path) -> None:
        if not any(self._is_relative_to(path, allowed) for allowed in self.allowlist):
            allowed = ", ".join(str(item) for item in self.allowlist)
            raise PermissionError(f"path is outside workspace allowlist: {path}; allowed: {allowed}")

    @staticmethod
    def _is_relative_to(path: pathlib.Path, parent: pathlib.Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

