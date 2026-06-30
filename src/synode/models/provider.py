from __future__ import annotations

from typing import Any, Protocol

from pydantic import BaseModel, Field


class ModelRequest(BaseModel):
    role: str
    prompt: str
    context: dict[str, Any] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)


class ModelResponse(BaseModel):
    content: str
    structured: dict[str, Any] = Field(default_factory=dict)


class ModelProvider(Protocol):
    name: str

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError


class FakeModelProvider:
    name = "fake"

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        summary = request.prompt.strip().splitlines()[0][:160] if request.prompt.strip() else "No prompt"
        return ModelResponse(
            content=f"[fake:{request.role}] {summary}",
            structured={"provider": self.name, "role": request.role, "tools": request.tools},
        )


class UnconfiguredModelProvider:
    def __init__(self, name: str):
        self.name = name

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        raise RuntimeError(
            f"model provider '{self.name}' is not configured. Configure a concrete provider before use."
        )


class ModelProviderRegistry:
    def __init__(self) -> None:
        self._providers: dict[str, ModelProvider] = {"fake": FakeModelProvider()}

    def register(self, provider: ModelProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> ModelProvider:
        if name in self._providers:
            return self._providers[name]
        return UnconfiguredModelProvider(name)

