from __future__ import annotations

import re
from html.parser import HTMLParser
from typing import Any

import httpx

from synode.domain.models import ToolResult, ToolRisk
from synode.infrastructure.tools.base import ToolContext


class TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = data.strip()
        if text:
            self.parts.append(text)

    def text(self) -> str:
        return re.sub(r"\s+", " ", " ".join(self.parts)).strip()


class WebSearchTool:
    name = "native.web_search"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.NETWORK

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        query = str(arguments.get("query", "")).strip()
        if not query:
            return ToolResult(tool_name=self.name, ok=False, risk=ToolRisk.NETWORK, error="query is required")
        limit = int(arguments.get("limit", 5))
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                response = await client.get(
                    f"{context.settings.searxng_url.rstrip('/')}/search",
                    params={"q": query, "format": "json"},
                )
                response.raise_for_status()
                payload = response.json()
        except httpx.HTTPError as exc:
            return ToolResult(tool_name=self.name, ok=False, risk=ToolRisk.NETWORK, error=f"search failed: {exc}")
        results = [
            {"title": item.get("title"), "url": item.get("url"), "content": item.get("content")}
            for item in payload.get("results", [])[:limit]
        ]
        return ToolResult(tool_name=self.name, ok=True, risk=ToolRisk.NETWORK, output={"query": query, "results": results})


class WebFetchTool:
    name = "native.web_fetch"

    def classify(self, arguments: dict[str, Any]) -> ToolRisk:
        return ToolRisk.NETWORK

    async def run(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        url = str(arguments.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            return ToolResult(tool_name=self.name, ok=False, risk=ToolRisk.NETWORK, error="http(s) URL is required")
        try:
            async with httpx.AsyncClient(timeout=20, follow_redirects=True) as client:
                response = await client.get(url)
                response.raise_for_status()
        except httpx.HTTPError as exc:
            return ToolResult(tool_name=self.name, ok=False, risk=ToolRisk.NETWORK, error=f"fetch failed: {exc}")
        parser = TextExtractor()
        parser.feed(response.text[:500_000])
        return ToolResult(
            tool_name=self.name,
            ok=True,
            risk=ToolRisk.NETWORK,
            output={"url": str(response.url), "status_code": response.status_code, "text": parser.text()[:12000]},
        )
