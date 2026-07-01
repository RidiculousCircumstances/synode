from __future__ import annotations

import pathlib

import pytest

from synode.infrastructure.config import Settings
from synode.infrastructure.observability import Observability


def test_observability_disabled_is_explicit_noop(settings: Settings) -> None:
    observability = Observability(settings)

    assert observability.enabled is False
    assert observability.create_trace_id() is None
    with observability.observation("test", None):
        observability.update_current_span(output={"ok": True})


def test_langfuse_enabled_requires_credentials(tmp_path: pathlib.Path) -> None:
    settings = Settings(
        database_url="sqlite+aiosqlite:///:memory:",
        enable_postgres_checkpointer=False,
        workspace_allowlist=str(tmp_path),
        mcp_config_path=tmp_path / ".mcp.json",
        langfuse_enabled=True,
    )

    with pytest.raises(RuntimeError, match="Langfuse is enabled"):
        Observability(settings)
