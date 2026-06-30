from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SYNODE_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+asyncpg://synode:synode@127.0.0.1:15432/synode"
    checkpoint_database_url: str = "postgresql://synode:synode@127.0.0.1:15432/synode?sslmode=disable"
    enable_postgres_checkpointer: bool = True
    searxng_url: str = "http://127.0.0.1:18080"
    model_provider: str = "ollama"
    ollama_base_url: str = "http://127.0.0.1:11434"
    ollama_model: str = "qwen2.5-coder:7b"
    model_timeout_seconds: float = 60.0
    secrets_key: str | None = None
    workspace_allowlist: str = "/home/rd/proj,/tmp"
    mcp_config_path: Path = Path(".mcp.json")
    shell_timeout_seconds: float = 20.0
    sandbox_backend: Literal["process", "none"] = "process"
    sandbox_cpu_seconds: int = 30
    sandbox_memory_mb: int = 512
    sandbox_disk_mb: int = 1024
    sandbox_output_max_bytes: int = 12000
    worker_id: str | None = None
    worker_poll_interval_seconds: float = 1.0
    worker_heartbeat_interval_seconds: float = 5.0
    worker_stale_after_seconds: float = 120.0
    worker_concurrency: int = 1
    run_event_retention_days: int = 30
    model_delta_retention_days: int = 7
    tool_audit_retention_days: int = 30
    artifact_retention_days: int = 30
    archived_thread_retention_days: int = 90
    max_event_payload_bytes: int = 65536
    max_tool_audit_payload_bytes: int = 65536
    max_artifact_payload_bytes: int = 262144
    db_statement_timeout_ms: int = 5000
    db_row_limit: int = 200
    api_cors_origins: str = "http://127.0.0.1:3000,http://localhost:3000"
    api_cors_origin_regex: str | None = (
        r"https?://("
        r"localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\]|"
        r"10(?:\.\d{1,3}){3}|192\.168(?:\.\d{1,3}){2}|"
        r"172\.(?:1[6-9]|2\d|3[0-1])(?:\.\d{1,3}){2}|"
        r"[a-zA-Z0-9.-]+\.local"
        r")(?::\d+)?"
    )
    langfuse_enabled: bool = False
    langfuse_base_url: str | None = None
    langfuse_public_key: str | None = None
    langfuse_secret_key: str | None = None

    @property
    def workspace_allowlist_paths(self) -> list[Path]:
        return [Path(part.strip()) for part in self.workspace_allowlist.split(",") if part.strip()]

    @property
    def api_cors_origin_list(self) -> list[str]:
        return [origin.strip() for origin in self.api_cors_origins.split(",") if origin.strip()]

    def validate_startup(self) -> None:
        if not self.database_url.strip():
            raise RuntimeError("SYNODE_DATABASE_URL is required")
        if not self.workspace_allowlist_paths:
            raise RuntimeError("SYNODE_WORKSPACE_ALLOWLIST must include at least one path")
        if self.shell_timeout_seconds <= 0:
            raise RuntimeError("SYNODE_SHELL_TIMEOUT_SECONDS must be greater than zero")
        if self.worker_concurrency < 1:
            raise RuntimeError("SYNODE_WORKER_CONCURRENCY must be at least 1")
        positive_limits = {
            "SYNODE_SANDBOX_CPU_SECONDS": self.sandbox_cpu_seconds,
            "SYNODE_SANDBOX_MEMORY_MB": self.sandbox_memory_mb,
            "SYNODE_SANDBOX_DISK_MB": self.sandbox_disk_mb,
            "SYNODE_SANDBOX_OUTPUT_MAX_BYTES": self.sandbox_output_max_bytes,
            "SYNODE_MAX_EVENT_PAYLOAD_BYTES": self.max_event_payload_bytes,
            "SYNODE_MAX_TOOL_AUDIT_PAYLOAD_BYTES": self.max_tool_audit_payload_bytes,
            "SYNODE_MAX_ARTIFACT_PAYLOAD_BYTES": self.max_artifact_payload_bytes,
        }
        invalid = [name for name, value in positive_limits.items() if value <= 0]
        if invalid:
            raise RuntimeError(f"runtime limit settings must be positive: {', '.join(invalid)}")


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
