from __future__ import annotations

import json

import httpx
import pytest

from synode.models.errors import StructuredOutputValidationError
from synode.models.provider import ModelRequest, OllamaProvider
from synode.runtime.decisions import RiskLevel, SupervisorDecision
from synode.schemas import RoleName


async def test_ollama_provider_maps_structured_response(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "selected_roles": [RoleName.DATA_ANALYST.value],
        "plan": [{"role": RoleName.DATA_ANALYST.value, "task": "Analyze data", "tool_calls": []}],
        "confidence": "high",
        "risk_level": RiskLevel.ANALYSIS.value,
        "reasoning_summary": "Data task.",
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"message": {"content": json.dumps(payload)}}))
    _patch_async_client(monkeypatch, transport)
    provider = OllamaProvider("http://ollama.test", "qwen2.5-coder:7b")

    response = await provider.invoke(
        ModelRequest(role="supervisor", prompt="plan", response_schema=SupervisorDecision)
    )

    assert response.provider == "ollama"
    assert response.model == "qwen2.5-coder:7b"
    assert response.structured["selected_roles"] == [RoleName.DATA_ANALYST.value]


async def test_ollama_provider_sends_request_context(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_payload: dict[str, object] = {}

    def handle(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(200, json={"message": {"content": "ok"}})

    _patch_async_client(monkeypatch, httpx.MockTransport(handle))
    provider = OllamaProvider("http://ollama.test", "qwen2.5-coder:7b")

    await provider.invoke(
        ModelRequest(
            role="coder",
            prompt="Answer the follow-up",
            context={"conversation_context": [{"author_type": "user", "content": "Earlier request"}]},
        )
    )

    messages = captured_payload["messages"]
    assert isinstance(messages, list)
    assert "Context JSON" in messages[0]["content"]
    assert "Earlier request" in messages[0]["content"]


async def test_ollama_provider_streams_token_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    captured_payload: dict[str, object] = {}
    body = (
        b'{"message":{"content":"hello "},"done":false}\n'
        b'{"message":{"content":"world"},"done":false}\n'
        b'{"done":true,"prompt_eval_count":3,"eval_count":2}\n'
    )

    def handle(request: httpx.Request) -> httpx.Response:
        captured_payload.update(json.loads(request.content))
        return httpx.Response(200, content=body)

    _patch_async_client(monkeypatch, httpx.MockTransport(handle))
    provider = OllamaProvider("http://ollama.test", "qwen2.5-coder:7b")
    deltas: list[str] = []

    async def on_delta(delta: str) -> None:
        deltas.append(delta)

    response = await provider.invoke_stream(
        ModelRequest(role="coder", prompt="Stream a concise answer"),
        on_delta,
    )

    assert captured_payload["stream"] is True
    assert deltas == ["hello ", "world"]
    assert response.content == "hello world"
    assert response.input_tokens == 3
    assert response.output_tokens == 2
    assert response.total_tokens == 5


async def test_ollama_provider_rejects_invalid_structured_response(monkeypatch: pytest.MonkeyPatch) -> None:
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={"message": {"content": "not json"}}))
    _patch_async_client(monkeypatch, transport)
    provider = OllamaProvider("http://ollama.test", "qwen2.5-coder:7b")

    with pytest.raises(StructuredOutputValidationError):
        await provider.invoke(ModelRequest(role="supervisor", prompt="plan", response_schema=SupervisorDecision))


def test_supervisor_decision_rejects_system_roles() -> None:
    payload = {
        "selected_roles": [RoleName.DATA_ANALYST.value, RoleName.REVIEWER.value],
        "plan": [
            {"role": RoleName.DATA_ANALYST.value, "task": "Analyze data", "tool_calls": []},
            {"role": RoleName.REVIEWER.value, "task": "Review", "tool_calls": []},
        ],
        "confidence": "high",
        "risk_level": RiskLevel.ANALYSIS.value,
        "reasoning_summary": "Invalid system role.",
    }

    with pytest.raises(ValueError, match="worker roles only"):
        SupervisorDecision.model_validate(payload)


def _patch_async_client(monkeypatch: pytest.MonkeyPatch, transport: httpx.MockTransport) -> None:
    original = httpx.AsyncClient

    class ClientFactory:
        def __init__(self, *args: object, **kwargs: object):
            self.client = original(transport=transport)

        async def __aenter__(self) -> httpx.AsyncClient:
            return self.client

        async def __aexit__(self, *args: object) -> None:
            await self.client.aclose()

    monkeypatch.setattr("synode.models.provider.httpx.AsyncClient", ClientFactory)
