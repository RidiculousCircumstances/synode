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
