from __future__ import annotations

import json
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from synode.config import Settings
from synode.models.errors import (
    ModelProviderUnavailableError,
    ModelResponseError,
    StructuredOutputValidationError,
)
from synode.runtime.decisions import (
    CodingInspection,
    PatchProposal,
    ReviewerDecision,
    ReviewerVerdict,
    RiskLevel,
    SupervisorDecision,
    VerificationPlan,
    WorkerPlanStep,
)
from synode.runtime.routing import select_worker_roles
from synode.schemas import RoleName, ToolCall


class ModelRequest(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    role: str
    prompt: str
    messages: list[dict[str, str]] = Field(default_factory=list)
    context: dict[str, Any] = Field(default_factory=dict)
    tools: list[str] = Field(default_factory=list)
    response_schema: type[BaseModel] | None = Field(default=None, exclude=True)
    temperature: float = 0.1
    timeout_seconds: float | None = None
    model_options: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    content: str
    structured: dict[str, Any] = Field(default_factory=dict)
    provider: str
    model: str | None = None


class ModelHealth(BaseModel):
    provider: str
    ok: bool
    model: str | None = None
    error: str | None = None


class ModelProvider(Protocol):
    name: str

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        raise NotImplementedError

    async def health(self) -> ModelHealth:
        raise NotImplementedError


class FakeModelProvider:
    name = "fake"

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        if request.response_schema is not None:
            structured = self._structured_response(request)
            return ModelResponse(
                content=json.dumps(structured, ensure_ascii=False),
                structured=structured,
                provider=self.name,
                model="fake",
            )
        summary = request.prompt.strip().splitlines()[0][:160] if request.prompt.strip() else "No prompt"
        return ModelResponse(
            content=f"[fake:{request.role}] {summary}",
            structured={"provider": self.name, "role": request.role, "tools": request.tools},
            provider=self.name,
            model="fake",
        )

    async def health(self) -> ModelHealth:
        return ModelHealth(provider=self.name, ok=True, model="fake")

    def _structured_response(self, request: ModelRequest) -> dict[str, Any]:
        schema = request.response_schema
        if schema is SupervisorDecision:
            task = str(request.context.get("task") or request.prompt)
            roles = [RoleName(role) for role in select_worker_roles(task)]
            if request.context.get("mode") == "coding":
                roles = [RoleName.CODER]
            plan = [
                WorkerPlanStep(
                    role=role,
                    task=f"Handle {role.value} portion of the task.",
                    tool_calls=self._fake_tool_calls(role, request.prompt),
                )
                for role in roles
            ]
            decision = SupervisorDecision(
                selected_roles=roles,
                plan=plan,
                confidence="high",
                risk_level=RiskLevel.SMALL_CODE if RoleName.CODER in roles else RiskLevel.ANALYSIS,
                reasoning_summary="Deterministic fake supervisor decision for tests.",
            )
            return decision.model_dump(mode="json")
        if schema is ReviewerDecision:
            blockers = list(request.context.get("blockers", []))
            verdict = ReviewerVerdict.BLOCK if blockers else ReviewerVerdict.PROCEED
            reviewer_decision = ReviewerDecision(
                verdict=verdict,
                blockers=blockers,
                advisory_risks=list(request.context.get("advisory", [])),
                missing_evidence=[],
                required_next_actions=[],
                confidence="high",
            )
            return reviewer_decision.model_dump(mode="json")
        if schema is CodingInspection:
            inspection = CodingInspection(
                summary="Deterministic fake coding inspection.",
                relevant_files=["README.md"],
                observed_failures=[],
                proposed_test_commands=[["python", "-m", "pytest"]],
            )
            return inspection.model_dump(mode="json")
        if schema is PatchProposal:
            proposal = request.context.get("fake_patch_proposal")
            if isinstance(proposal, dict):
                return PatchProposal.model_validate(proposal).model_dump(mode="json")
            raise StructuredOutputValidationError("fake PatchProposal requires fake_patch_proposal context")
        if schema is VerificationPlan:
            commands = request.context.get("commands") or [["python", "-m", "pytest"]]
            verification_plan = VerificationPlan(commands=commands, reason="Deterministic fake verification plan.")
            return verification_plan.model_dump(mode="json")
        raise StructuredOutputValidationError(f"fake provider has no structured fixture for {schema}")

    @staticmethod
    def _fake_tool_calls(role: RoleName, prompt: str) -> list[ToolCall]:
        if role == RoleName.CODER:
            return [
                ToolCall(
                    name="native.fs_search",
                    arguments={"pattern": "TODO|FIXME|error|raise", "glob": "*.py", "max_matches": 20},
                ),
                ToolCall(name="native.git_status", arguments={}),
            ]
        if role == RoleName.DATA_ANALYST:
            return [ToolCall(name="native.data_profile", arguments={})]
        if role == RoleName.WEB_RESEARCHER:
            return [ToolCall(name="native.web_search", arguments={"query": prompt, "limit": 5})]
        if role == RoleName.DB_AGENT:
            return [ToolCall(name="native.db_readonly", arguments={})]
        return []


class OllamaProvider:
    name = "ollama"

    def __init__(self, base_url: str, model: str, timeout_seconds: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = timeout_seconds

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        messages = request.messages or [{"role": "user", "content": request.prompt}]
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": request.temperature, **request.model_options},
        }
        if request.response_schema is not None:
            payload["format"] = request.response_schema.model_json_schema()
            payload["messages"] = [
                *messages,
                {
                    "role": "user",
                    "content": "Return only JSON that validates against the provided schema.",
                },
            ]
        timeout = request.timeout_seconds or self.timeout_seconds
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                response = await client.post(f"{self.base_url}/api/chat", json=payload)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            raise ModelProviderUnavailableError(f"ollama request failed: {exc}") from exc
        try:
            body = response.json()
            content = body["message"]["content"]
        except (KeyError, TypeError, ValueError) as exc:
            raise ModelResponseError(f"ollama returned invalid response envelope: {response.text[:500]}") from exc
        if not isinstance(content, str):
            raise ModelResponseError("ollama message content is not a string")
        structured: dict[str, Any] = {}
        if request.response_schema is not None:
            structured = self._validate_structured(request.response_schema, content)
        return ModelResponse(content=content, structured=structured, provider=self.name, model=self.model)

    async def health(self) -> ModelHealth:
        try:
            async with httpx.AsyncClient(timeout=5) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            return ModelHealth(provider=self.name, ok=False, model=self.model, error=str(exc))
        models = {item.get("name") for item in payload.get("models", []) if isinstance(item, dict)}
        if self.model not in models:
            return ModelHealth(
                provider=self.name,
                ok=False,
                model=self.model,
                error=f"model is not installed in Ollama: {self.model}",
            )
        return ModelHealth(provider=self.name, ok=True, model=self.model)

    @staticmethod
    def _validate_structured(schema: type[BaseModel], content: str) -> dict[str, Any]:
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise StructuredOutputValidationError(f"model returned invalid JSON: {exc}") from exc
        try:
            validated = schema.model_validate(parsed)
        except ValidationError as exc:
            raise StructuredOutputValidationError(f"model JSON failed schema validation: {exc}") from exc
        return validated.model_dump(mode="json")


class UnconfiguredModelProvider:
    def __init__(self, name: str):
        self.name = name

    async def invoke(self, request: ModelRequest) -> ModelResponse:
        raise ModelProviderUnavailableError(
            f"model provider '{self.name}' is not configured. Configure a concrete provider before use."
        )

    async def health(self) -> ModelHealth:
        return ModelHealth(provider=self.name, ok=False, error="provider is not configured")


class ModelProviderRegistry:
    def __init__(self, settings: Settings | None = None) -> None:
        self._providers: dict[str, ModelProvider] = {"fake": FakeModelProvider()}
        if settings is not None:
            self.register(
                OllamaProvider(
                    base_url=settings.ollama_base_url,
                    model=settings.ollama_model,
                    timeout_seconds=settings.model_timeout_seconds,
                )
            )

    def register(self, provider: ModelProvider) -> None:
        self._providers[provider.name] = provider

    def get(self, name: str) -> ModelProvider:
        if name in self._providers:
            return self._providers[name]
        return UnconfiguredModelProvider(name)

    async def health(self) -> list[ModelHealth]:
        return [await provider.health() for provider in self._providers.values()]
