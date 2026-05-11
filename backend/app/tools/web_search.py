"""web_search tool: Tavily-backed web search."""

from __future__ import annotations

import time
from typing import Any

import httpx

from ..llm import LLMClient
from ..models import SharedContext, ToolResult
from ..settings import settings

TAVILY_URL = "https://api.tavily.com/search"
_STUB_KEYS = {"", "stub-not-used-in-slice-0"}


def _normalize_results(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("results") or []
    out: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        content = item.get("content") or ""
        out.append(
            {
                "title": item.get("title") or "",
                "url": item.get("url") or "",
                "snippet": content[:500],
                "relevance_score": item.get("score"),
            }
        )
    return out


async def run(ctx: SharedContext, llm: LLMClient) -> ToolResult:
    started = time.perf_counter()
    key = (settings.TAVILY_API_KEY or "").strip()
    if key in _STUB_KEYS:
        return ToolResult(
            tool_name="web_search",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message="TAVILY_API_KEY not configured",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    tool_input = ctx.agent_outputs.get("__tool_input__") or {}
    query = (tool_input.get("query") or ctx.query or "").strip()
    if not query:
        return ToolResult(
            tool_name="web_search",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message="empty query",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    body = {
        "api_key": key,
        "query": query,
        "max_results": 10,
        "search_depth": "basic",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.WEB_SEARCH_TIMEOUT_SECONDS) as client:
            resp = await client.post(TAVILY_URL, json=body)
    except httpx.TimeoutException:
        return ToolResult(
            tool_name="web_search",
            success=False,
            data=None,
            error_code="TIMEOUT",
            error_message="Tavily request timed out",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
    except httpx.HTTPError as exc:
        return ToolResult(
            tool_name="web_search",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message=f"{exc.__class__.__name__}: {exc}",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    latency_ms = (time.perf_counter() - started) * 1000.0

    if resp.status_code >= 400:
        return ToolResult(
            tool_name="web_search",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message=f"HTTP {resp.status_code}",
            latency_ms=latency_ms,
        )

    try:
        payload = resp.json()
    except ValueError:
        return ToolResult(
            tool_name="web_search",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message="non-JSON response body",
            latency_ms=latency_ms,
        )

    results = _normalize_results(payload)
    if not results:
        return ToolResult(
            tool_name="web_search",
            success=False,
            data=[],
            error_code="EMPTY",
            error_message="no results",
            latency_ms=latency_ms,
        )

    return ToolResult(
        tool_name="web_search",
        success=True,
        data=results,
        error_code=None,
        latency_ms=latency_ms,
    )
