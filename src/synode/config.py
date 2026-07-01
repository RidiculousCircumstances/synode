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
    mcp_proxy_base_url: str = "http://127.0.0.1:8787"
    mcp_proxy_session_ttl_seconds: int = 3600
    shell_timeout_seconds: float = 20.0
    sandbox_backend: Literal["process", "docker", "none"] = "process"
    sandbox_cpu_seconds: int = 30
    sandbox_memory_mb: int = 512
    sandbox_disk_mb: int = 1024
    sandbox_output_max_bytes: int = 12000
    sandbox_docker_image: str = "synode-sandbox:local"
    sandbox_docker_socket: str = "/var/run/docker.sock"
    sandbox_docker_network: str = "none"
    sandbox_docker_workdir: str = "/workspace"
    sandbox_docker_user: str = "1000:1000"
    sandbox_docker_cpus: float = 1.0
    sandbox_docker_pids_limit: int = 128
    sandbox_docker_tmpfs_mb: int = 64
    sandbox_docker_host_workspace: str | None = None
    sandbox_docker_container_workspace: str | None = None
    worker_id: str | None = None
    worker_poll_interval_seconds: float = 1.0
    worker_heartbeat_interval_seconds: float = 5.0
    worker_stale_after_seconds: float = 120.0
    worker_concurrency: int = 1
    run_queue_transport: Literal["procrastinate"] = "procrastinate"
    queue_database_url: str | None = None
    queue_name: str = "synode_runs"
    openhands_enabled: bool = False
    openhands_base_url: str | None = None
    openhands_api_key: str | None = None
    openhands_api_mode: Literal["agent_server", "cloud_v1"] = "agent_server"
    openhands_timeout_seconds: float = 120.0
    openhands_poll_interval_seconds: float = 1.0
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

    @property
    def resolved_queue_database_url(self) -> str:
        return self.queue_database_url or self.checkpoint_database_url

    def validate_startup(self) -> None:
        if not self.database_url.strip():
            raise RuntimeError("SYNODE_DATABASE_URL is required")
        if self.run_queue_transport != "procrastinate":
            raise RuntimeError(f"unsupported SYNODE_RUN_QUEUE_TRANSPORT: {self.run_queue_transport}")
        if not self.resolved_queue_database_url.strip():
            raise RuntimeError("SYNODE_QUEUE_DATABASE_URL must not be blank")
        if not self.resolved_queue_database_url.startswith(
            ("postgresql://", "postgresql+psycopg://", "postgresql+asyncpg://")
        ):
            raise RuntimeError("SYNODE_QUEUE_DATABASE_URL must use PostgreSQL for Procrastinate")
        if not self.queue_name.strip():
            raise RuntimeError("SYNODE_QUEUE_NAME must not be blank")
        if self.openhands_enabled:
            if not self.openhands_base_url or not self.openhands_base_url.strip():
                raise RuntimeError("SYNODE_OPENHANDS_BASE_URL is required when OpenHands is enabled")
            if self.openhands_timeout_seconds <= 0:
                raise RuntimeError("SYNODE_OPENHANDS_TIMEOUT_SECONDS must be greater than zero")
            if self.openhands_poll_interval_seconds <= 0:
                raise RuntimeError("SYNODE_OPENHANDS_POLL_INTERVAL_SECONDS must be greater than zero")
        if not self.workspace_allowlist_paths:
            raise RuntimeError("SYNODE_WORKSPACE_ALLOWLIST must include at least one path")
        if not self.mcp_proxy_base_url.strip():
            raise RuntimeError("SYNODE_MCP_PROXY_BASE_URL must not be blank")
        if self.mcp_proxy_session_ttl_seconds <= 0:
            raise RuntimeError("SYNODE_MCP_PROXY_SESSION_TTL_SECONDS must be greater than zero")
        if self.shell_timeout_seconds <= 0:
            raise RuntimeError("SYNODE_SHELL_TIMEOUT_SECONDS must be greater than zero")
        if self.worker_concurrency < 1:
            raise RuntimeError("SYNODE_WORKER_CONCURRENCY must be at least 1")
        positive_limits = {
            "SYNODE_SANDBOX_CPU_SECONDS": self.sandbox_cpu_seconds,
            "SYNODE_SANDBOX_MEMORY_MB": self.sandbox_memory_mb,
            "SYNODE_SANDBOX_DISK_MB": self.sandbox_disk_mb,
            "SYNODE_SANDBOX_OUTPUT_MAX_BYTES": self.sandbox_output_max_bytes,
            "SYNODE_SANDBOX_DOCKER_CPUS": self.sandbox_docker_cpus,
            "SYNODE_SANDBOX_DOCKER_PIDS_LIMIT": self.sandbox_docker_pids_limit,
            "SYNODE_SANDBOX_DOCKER_TMPFS_MB": self.sandbox_docker_tmpfs_mb,
            "SYNODE_MAX_EVENT_PAYLOAD_BYTES": self.max_event_payload_bytes,
            "SYNODE_MAX_TOOL_AUDIT_PAYLOAD_BYTES": self.max_tool_audit_payload_bytes,
            "SYNODE_MAX_ARTIFACT_PAYLOAD_BYTES": self.max_artifact_payload_bytes,
        }
        invalid = [name for name, value in positive_limits.items() if value <= 0]
        if invalid:
            raise RuntimeError(f"runtime limit settings must be positive: {', '.join(invalid)}")
        if self.sandbox_backend == "docker" and not self.sandbox_docker_image.strip():
            raise RuntimeError("SYNODE_SANDBOX_DOCKER_IMAGE is required for docker sandbox backend")
        if not self.sandbox_docker_socket.strip():
            raise RuntimeError("SYNODE_SANDBOX_DOCKER_SOCKET must not be blank")
        if "://" in self.sandbox_docker_socket and not self.sandbox_docker_socket.startswith("unix://"):
            raise RuntimeError("SYNODE_SANDBOX_DOCKER_SOCKET currently supports only unix socket paths")
        if not self.sandbox_docker_network.strip():
            raise RuntimeError("SYNODE_SANDBOX_DOCKER_NETWORK must not be blank")
        if not Path(self.sandbox_docker_workdir).is_absolute():
            raise RuntimeError("SYNODE_SANDBOX_DOCKER_WORKDIR must be an absolute container path")
        if bool(self.sandbox_docker_host_workspace) != bool(self.sandbox_docker_container_workspace):
            raise RuntimeError(
                "SYNODE_SANDBOX_DOCKER_HOST_WORKSPACE and "
                "SYNODE_SANDBOX_DOCKER_CONTAINER_WORKSPACE must be set together"
            )
        if self.sandbox_docker_container_workspace and not Path(
            self.sandbox_docker_container_workspace
        ).is_absolute():
            raise RuntimeError(
                "SYNODE_SANDBOX_DOCKER_CONTAINER_WORKSPACE must be an absolute container path"
            )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
