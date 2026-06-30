from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from sqlalchemy import select

from synode.config import Settings
from synode.persistence.database import Database
from synode.persistence.models import ApprovalRecord
from synode.persistence.repository import Repository
from synode.registry import RoleRegistry
from synode.schemas import ApprovalStatus, ToolCall, ToolResult, ToolRisk
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


class ToolExecutor:
    def __init__(self, database: Database, roles: RoleRegistry, tools: ToolRegistry, settings: Settings):
        self.database = database
        self.roles = roles
        self.tools = tools
        self.settings = settings
        self.workspace_policy = WorkspacePolicy(settings.workspace_allowlist_paths)

    async def execute(
        self,
        run_id: str,
        role_name: str,
        workspace: str | None,
        call: ToolCall,
        approved_approval_id: str | None = None,
    ) -> ToolResult:
        role = self.roles.get(role_name)
        tool = self.tools.get(call.name)
        risk = tool.classify(call.arguments)
        context = ToolContext(
            run_id=run_id,
            role=role_name,
            workspace=workspace,
            settings=self.settings,
            workspace_policy=self.workspace_policy,
        )

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
                    run_id, role_name, call.name, risk, "denied", call.arguments, result.model_dump(mode="json")
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
                return result

        result = await tool.run(context, call.arguments)
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
