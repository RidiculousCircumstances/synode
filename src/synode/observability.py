from __future__ import annotations

from contextlib import nullcontext
from typing import Any, ContextManager

from synode.config import Settings


class Observability:
    def __init__(self, settings: Settings):
        self.enabled = settings.langfuse_enabled
        self._client: Any | None = None
        if not self.enabled:
            return

        missing = [
            name
            for name, value in {
                "SYNODE_LANGFUSE_BASE_URL": settings.langfuse_base_url,
                "SYNODE_LANGFUSE_PUBLIC_KEY": settings.langfuse_public_key,
                "SYNODE_LANGFUSE_SECRET_KEY": settings.langfuse_secret_key,
            }.items()
            if not value
        ]
        if missing:
            raise RuntimeError(f"Langfuse is enabled but required settings are missing: {', '.join(missing)}")

        try:
            from langfuse import Langfuse
        except ImportError as exc:
            raise RuntimeError("Langfuse is enabled but the 'langfuse' package is not installed") from exc

        self._client = Langfuse(
            public_key=settings.langfuse_public_key,
            secret_key=settings.langfuse_secret_key,
            base_url=settings.langfuse_base_url,
        )

    def create_trace_id(self, seed: str | None = None) -> str | None:
        if not self.enabled:
            return None
        return self._require_client().create_trace_id(seed=seed)

    def observation(
        self,
        name: str,
        trace_id: str | None,
        *,
        as_type: str = "span",
        input_payload: Any | None = None,
        metadata: dict[str, Any] | None = None,
        model: str | None = None,
        usage_details: dict[str, int] | None = None,
    ) -> ContextManager[Any]:
        if not self.enabled:
            return nullcontext()
        trace_context = {"trace_id": trace_id} if trace_id else None
        return self._require_client().start_as_current_observation(
            trace_context=trace_context,
            name=name,
            as_type=as_type,
            input=input_payload,
            metadata=metadata,
            model=model,
            usage_details=usage_details,
        )

    def update_current_span(
        self,
        *,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
        status_message: str | None = None,
    ) -> None:
        if self.enabled:
            self._require_client().update_current_span(
                output=output,
                metadata=metadata,
                level=level,
                status_message=status_message,
            )

    def update_current_generation(
        self,
        *,
        output: Any | None = None,
        metadata: dict[str, Any] | None = None,
        level: str | None = None,
        status_message: str | None = None,
        model: str | None = None,
        usage_details: dict[str, int] | None = None,
    ) -> None:
        if self.enabled:
            self._require_client().update_current_generation(
                output=output,
                metadata=metadata,
                level=level,
                status_message=status_message,
                model=model,
                usage_details=usage_details,
            )

    def trace_url(self, trace_id: str | None) -> str | None:
        if not self.enabled or not trace_id:
            return None
        return self._require_client().get_trace_url(trace_id=trace_id)

    def flush(self) -> None:
        if self.enabled:
            self._require_client().flush()

    def shutdown(self) -> None:
        if self.enabled:
            self._require_client().shutdown()

    def _require_client(self) -> Any:
        if self._client is None:
            raise RuntimeError("observability client is not initialized")
        return self._client
