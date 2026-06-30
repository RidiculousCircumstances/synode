from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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
    workspace_allowlist: str = "/home/rd/proj,/tmp"
    mcp_config_path: Path = Path(".mcp.json")
    shell_timeout_seconds: float = 20.0
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


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
