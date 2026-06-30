from __future__ import annotations

import re
from typing import Any

import asyncpg

from synode.persistence.urls import to_async_database_url
from synode.schemas import ToolResult, ToolRisk
from synode.tools.base import ToolContext

MUTATING_SQL_RE = re.compile(
    r"\b(insert|update|delete|merge|alter|drop|truncate|create|grant|revoke|vacuum|call|copy)\b",
    re.IGNORECASE,
)


class DatabaseReadonlyTool:
    name = "native.db_readonly"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        sql = str(arguments.get("sql", "")).strip()
        if sql and MUTATING_SQL_RE.search(sql):
            return ToolRisk.WRITE
        return ToolRisk.READ

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        sql = str(arguments.get("sql", "")).strip()
        database_url = str(arguments.get("database_url") or context.settings.database_url)
        if not to_async_database_url(database_url).startswith("postgresql+asyncpg://"):
            return ToolResult(tool_name=self.name, ok=False, error="db_readonly MVP supports PostgreSQL URLs")
        if not sql:
            sql = (
                "select table_schema, table_name "
                "from information_schema.tables "
                "where table_schema not in ('pg_catalog', 'information_schema') "
                "order by table_schema, table_name"
            )
        guard_error = self._guard_sql(sql)
        if guard_error:
            return ToolResult(tool_name=self.name, ok=False, error=guard_error, risk=self.classify(arguments))
        row_limit = int(arguments.get("row_limit", context.settings.db_row_limit))
        query = f"select * from ({sql.rstrip(';')}) synode_readonly_query limit {row_limit}"
        conn = await asyncpg.connect(to_async_database_url(database_url).replace("postgresql+asyncpg://", "postgresql://"))
        try:
            async with conn.transaction(readonly=True):
                await conn.execute(f"set local statement_timeout = {int(context.settings.db_statement_timeout_ms)}")
                rows = await conn.fetch(query)
        finally:
            await conn.close()
        return ToolResult(
            tool_name=self.name,
            ok=True,
            output={"row_limit": row_limit, "rows": [dict(row) for row in rows]},
        )

    @staticmethod
    def _guard_sql(sql: str) -> str | None:
        if ";" in sql.rstrip(";"):
            return "multi-statement SQL is not allowed"
        if "--" in sql or "/*" in sql:
            return "SQL comments are not allowed"
        lowered = sql.lower().strip()
        if not lowered.startswith(("select", "with", "explain")):
            return "only SELECT, WITH, and EXPLAIN statements are allowed"
        if MUTATING_SQL_RE.search(sql):
            return "mutating SQL is not allowed"
        return None

