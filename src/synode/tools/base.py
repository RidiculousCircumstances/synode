from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol
from urllib.parse import urljoin

from sqlalchemy import select

from synode.config import Settings
from synode.observability import Observability
from synode.persistence.database import Database
from synode.persistence.models import ApprovalRecord
from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.schemas import ApprovalStatus, ToolCall, ToolResult, ToolRisk
from synode.tools.catalog import tool_catalog_entry, tool_catalog_for, tool_input_schema
from synode.tools.sandbox import SandboxRunner, SandboxUnavailable
from synode.tools.workspace import WorkspacePolicy


class BaseTool(Protocol):
    name: str

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        raise NotImplementedError

    async def run(self, context: "ToolContext", arguments: dict[str, Any]) -> ToolResult:
        raise NotImplementedError


@dataclass(frozen=True)
class ToolContext:
    run_id: str
    role: str
    workspace: str | None
    settings: Settings
    workspace_policy: WorkspacePolicy
    sandbox: SandboxRunner


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        if tool.name in self._tools:
            raise ValueError(f"duplicate tool: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, name: str) -> BaseTool:
        try:
            return self._tools[name]
        except KeyError as exc:
            raise LookupError(f"unknown tool: {name}") from exc

    def list_names(self) -> list[str]:
        return sorted(self._tools)

    def unregister_prefix(self, prefix: str) -> None:
        for name in [name for name in self._tools if name.startswith(prefix)]:
            del self._tools[name]


@dataclass(frozen=True)
class MCPProxySession:
    session_id: str
    url: str
    token: str
    tools: list[str]


class ToolGateway:
    def __init__(
        self,
        database: Database,
        roles: RoleRegistry,
        tools: ToolRegistry,
        settings: Settings,
        observability: Observability | None = None,
    ):
        self.database = database
        self.roles = roles
        self.tools = tools
        self.settings = settings
        self.observability = observability or Observability(settings)
        self.workspace_policy = WorkspacePolicy(settings.workspace_allowlist_paths)
        self.sandbox = SandboxRunner(settings)

    def allowed_tool_names(self, role_name: str) -> list[str]:
        role = self.roles.get(role_name)
        return [name for name in self.tools.list_names() if role.allows_tool(name)]

    def tool_catalog(self, role_name: str) -> list[dict[str, Any]]:
        return tool_catalog_for(self.allowed_tool_names(role_name))

    async def create_proxy_session(
        self,
        *,
        run_id: str,
        thread_id: str,
        node_id: str,
        role_name: str,
        backend_id: str,
        workspace: str | None,
    ) -> MCPProxySession:
        token = secrets.token_urlsafe(32)
        token_hash = _token_hash(token)
        expires_at = datetime.now(UTC) + timedelta(seconds=self.settings.mcp_proxy_session_ttl_seconds)
        tools = self.allowed_tool_names(role_name)
        async with self.database.session() as session:
            repo = Repository(session)
            record = await repo.create_mcp_proxy_session(
                run_id=run_id,
                thread_id=thread_id,
                node_id=node_id,
                role=role_name,
                backend_id=backend_id,
                workspace=workspace,
                allowed_tools=tools,
                token_hash=token_hash,
                expires_at=expires_at,
            )
        base_url = self.settings.mcp_proxy_base_url.rstrip("/") + "/"
        return MCPProxySession(
            session_id=record.id,
            url=urljoin(base_url, f"mcp/proxy/{record.id}"),
            token=token,
            tools=tools,
        )

    async def handle_mcp_proxy_request(
        self,
        *,
        session_id: str,
        token: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        request_id = payload.get("id")
        method = str(payload.get("method") or "")
        raw_params = payload.get("params")
        params: dict[str, Any] = raw_params if isinstance(raw_params, dict) else {}
        try:
            result = await self._handle_proxy_method(session_id, token, method, params)
            if request_id is None:
                return None
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except Exception as exc:
            if request_id is None:
                raise
            return {
                "jsonrpc": "2.0",
                "id": request_id,
                "error": {
                    "code": _json_rpc_error_code(exc),
                    "message": str(exc),
                },
            }

    async def _handle_proxy_method(
        self,
        session_id: str,
        token: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        record = await self._validated_proxy_session(session_id, token)
        if method == "initialize":
            return {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "synode-mcp-proxy", "version": "0.1.0"},
            }
        if method in {"notifications/initialized", "ping"}:
            return {}
        if method == "tools/list":
            return {
                "tools": [
                    {
                        "name": name,
                        "description": str(tool_catalog_entry(name)["description"]),
                        "inputSchema": tool_input_schema(name),
                    }
                    for name in sorted(record.allowed_tools or [])
                    if name in self.tools.list_names()
                ]
            }
        if method == "tools/call":
            tool_name = str(params.get("name") or "")
            arguments = params.get("arguments")
            if not isinstance(arguments, dict):
                arguments = {}
            if tool_name not in set(record.allowed_tools or []):
                raise PermissionError(f"tool is not allowed for proxy session: {tool_name}")
            result = await self.execute(
                record.run_id,
                record.role,
                record.workspace,
                ToolCall(name=tool_name, arguments=arguments),
            )
            return {
                "content": [
                    {
                        "type": "text",
                        "text": result.model_dump_json(),
                    }
                ],
                "isError": not result.ok,
                "structuredContent": result.model_dump(mode="json"),
            }
        raise LookupError(f"unsupported MCP proxy method: {method}")

    async def _validated_proxy_session(self, session_id: str, token: str) -> Any:
        if not token:
            raise PermissionError("MCP proxy bearer token is required")
        async with self.database.session() as session:
            repo = Repository(session)
            record = await repo.get_mcp_proxy_session(session_id)
            if record is None:
                raise LookupError(f"MCP proxy session not found: {session_id}")
            if _is_expired(record.expires_at):
                raise PermissionError(f"MCP proxy session expired: {session_id}")
            if not secrets.compare_digest(record.token_hash, _token_hash(token)):
                raise PermissionError("invalid MCP proxy token")
            await repo.touch_mcp_proxy_session(session_id)
            return record

    async def execute(
        self,
        run_id: str,
        role_name: str,
        workspace: str | None,
        call: ToolCall,
        approved_approval_id: str | None = None,
    ) -> ToolResult:
        trace_id = await self._get_trace_id(run_id)
        role = self.roles.get(role_name)
        tool = self.tools.get(call.name)
        risk = tool.classify(call.arguments)
        context = ToolContext(
            run_id=run_id,
            role=role_name,
            workspace=workspace,
            settings=self.settings,
            workspace_policy=self.workspace_policy,
            sandbox=self.sandbox,
        )
        with self.observability.observation(
            f"tool.{call.name}",
            trace_id,
            as_type="tool",
            input_payload=call.arguments,
            metadata={"run_id": run_id, "role": role_name, "risk": risk.value},
        ):
            async with self.database.session() as session:
                repo = Repository(session)
                if not role.allows_tool(call.name):
                    result = ToolResult(
                        tool_name=call.name,
                        ok=False,
                        risk=risk,
                        error=f"role '{role_name}' is not allowed to use tool '{call.name}'",
                    )
                    await repo.add_tool_audit(
                        run_id,
                        role_name,
                        call.name,
                        risk,
                        "denied",
                        call.arguments,
                        result.model_dump(mode="json"),
                    )
                    self.observability.update_current_span(
                        output=result.model_dump(mode="json"),
                        level="ERROR",
                        status_message=result.error,
                    )
                    return result

                if self._requires_approval(risk) and approved_approval_id is None:
                    approved_approval_id = await self._find_existing_approval(
                        repo, run_id, call.name, role_name, call.arguments
                    )

                if self._requires_approval(risk) and approved_approval_id is None:
                    approval = await repo.create_approval(
                        run_id=run_id,
                        tool_name=call.name,
                        action=risk.value,
                        reason=f"Tool '{call.name}' requested {risk.value} access.",
                        payload={"role": role_name, "arguments": call.arguments},
                    )
                    result = ToolResult(
                        tool_name=call.name,
                        ok=False,
                        risk=risk,
                        error="approval required",
                        approval_id=approval.id,
                    )
                    await repo.add_tool_audit(
                        run_id,
                        role_name,
                        call.name,
                        risk,
                        "approval_required",
                        call.arguments,
                        result.model_dump(mode="json"),
                        approval_id=approval.id,
                    )
                    self.observability.update_current_span(
                        output=result.model_dump(mode="json"),
                        level="WARNING",
                        status_message="approval required",
                    )
                    return result

            if risk in {ToolRisk.WRITE, ToolRisk.DESTRUCTIVE}:
                try:
                    self.sandbox.ensure_available()
                except SandboxUnavailable as exc:
                    result = ToolResult(
                        tool_name=call.name,
                        ok=False,
                        risk=risk,
                        error=f"sandbox unavailable: {exc}",
                    )
                    async with self.database.session() as session:
                        repo = Repository(session)
                        await repo.add_tool_audit(
                            run_id,
                            role_name,
                            call.name,
                            risk,
                            "sandbox_unavailable",
                            call.arguments,
                            result.model_dump(mode="json"),
                            approval_id=approved_approval_id,
                        )
                    self.observability.update_current_span(
                        output=result.model_dump(mode="json"),
                        level="ERROR",
                        status_message=result.error,
                    )
                    return result

            try:
                result = await tool.run(context, call.arguments)
            except Exception as exc:
                result = ToolResult(
                    tool_name=call.name,
                    ok=False,
                    risk=risk,
                    error=f"tool execution failed ({exc.__class__.__name__}): {exc}",
                )
            async with self.database.session() as session:
                repo = Repository(session)
                await repo.add_tool_audit(
                    run_id,
                    role_name,
                    call.name,
                    result.risk,
                    "ok" if result.ok else "error",
                    call.arguments,
                    result.model_dump(mode="json"),
                    approval_id=approved_approval_id,
                )
            self.observability.update_current_span(
                output=result.model_dump(mode="json"),
                level=None if result.ok else "ERROR",
                status_message=result.error,
            )
            return result

    @staticmethod
    def _requires_approval(risk: ToolRisk) -> bool:
        return risk in {ToolRisk.WRITE, ToolRisk.DESTRUCTIVE}

    @staticmethod
    async def _find_existing_approval(
        repo: Repository, run_id: str, tool_name: str, role_name: str, arguments: dict[str, Any]
    ) -> str | None:
        result = await repo.session.execute(
            select(ApprovalRecord)
            .where(
                ApprovalRecord.run_id == run_id,
                ApprovalRecord.tool_name == tool_name,
                ApprovalRecord.status == ApprovalStatus.APPROVED.value,
            )
            .order_by(ApprovalRecord.created_at.desc())
        )
        expected = {"role": role_name, "arguments": arguments}
        for approval in result.scalars().all():
            if approval.payload == expected:
                return approval.id
        return None

    async def _get_trace_id(self, run_id: str) -> str | None:
        if not self.observability.enabled:
            return None
        async with self.database.session() as session:
            repo = Repository(session)
            run = await repo.get_run(run_id)
            if run is None:
                raise LookupError(f"run not found: {run_id}")
            return run.observability_trace_id


class ToolExecutor(ToolGateway):
    pass


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _is_expired(expires_at: datetime) -> bool:
    current = datetime.now(UTC)
    if expires_at.tzinfo is None:
        current = current.replace(tzinfo=None)
    return expires_at <= current


def _json_rpc_error_code(exc: Exception) -> int:
    if isinstance(exc, PermissionError):
        return -32001
    if isinstance(exc, LookupError):
        return -32601
    return -32000
