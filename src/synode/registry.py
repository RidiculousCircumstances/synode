from __future__ import annotations

import builtins
from dataclasses import dataclass
from importlib import resources
from typing import Any

import yaml


@dataclass(frozen=True)
class RoleSpec:
    name: str
    mission: str
    non_goals: list[str]
    allowed_tools: list[str]
    requires_approval_for: list[str]
    output_contract: str

    def allows_tool(self, tool_name: str) -> bool:
        for allowed in self.allowed_tools:
            if allowed.endswith(".*") and tool_name.startswith(allowed[:-1]):
                return True
            if allowed == tool_name:
                return True
        return False


class RoleRegistry:
    def __init__(self, roles: dict[str, RoleSpec]):
        self._roles = roles

    @classmethod
    def load_builtin(cls) -> "RoleRegistry":
        role_files = [
            item for item in resources.files("synode.agents").iterdir() if item.name.endswith(".yaml")
        ]
        roles: dict[str, RoleSpec] = {}
        for role_file in sorted(role_files, key=lambda item: item.name):
            data = yaml.safe_load(role_file.read_text(encoding="utf-8"))
            role = RoleSpec(
                name=str(data["name"]),
                mission=str(data["mission"]),
                non_goals=list(data.get("non_goals", [])),
                allowed_tools=list(data.get("allowed_tools", [])),
                requires_approval_for=list(data.get("requires_approval_for", [])),
                output_contract=str(data.get("output_contract", "")),
            )
            roles[role.name] = role
        return cls(roles)

    @classmethod
    def from_specs(cls, specs: list[RoleSpec]) -> "RoleRegistry":
        return cls({spec.name: spec for spec in specs})

    def get(self, name: str) -> RoleSpec:
        try:
            return self._roles[name]
        except KeyError as exc:
            raise LookupError(f"unknown role: {name}") from exc

    def list(self) -> list[RoleSpec]:
        return sorted(self._roles.values(), key=lambda role: role.name)

    def as_public(self) -> builtins.list[dict[str, Any]]:
        return [
            {
                "name": role.name,
                "mission": role.mission,
                "non_goals": role.non_goals,
                "allowed_tools": role.allowed_tools,
                "requires_approval_for": role.requires_approval_for,
                "output_contract": role.output_contract,
            }
            for role in self.list()
        ]
