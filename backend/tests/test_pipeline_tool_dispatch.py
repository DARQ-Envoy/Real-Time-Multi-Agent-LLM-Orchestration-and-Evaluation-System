"""Verifies pipeline._dispatch_tool_calls drains planned tool_calls per-agent and
records side effects (tool_calls row + agent_outputs["tools"] bucket) BEFORE the
matching agent runs.
"""

from __future__ import annotations

from typing import Any

import pytest

from app import pipeline
from app.models import PlannedToolCall, RoutingPlan, ToolResult


async def _fake_tool(ctx: Any, llm: Any) -> ToolResult:
    return ToolResult(
        tool_name="web_search",
        success=True,
        data=[{"title": "T", "url": "u", "snippet": "s", "relevance_score": 0.5}],
        error_code=None,
        latency_ms=1.0,
    )


@pytest.fixture(autouse=True)
def _patch_registry(monkeypatch):
    monkeypatch.setitem(pipeline.TOOL_REGISTRY, "web_search", _fake_tool)


async def test_dispatch_writes_tool_call_row_and_bucket(
    monkeypatch, shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    shared_ctx.routing_plan = RoutingPlan(
        agent_sequence=["rag", "synthesis"],
        tool_calls=[
            PlannedToolCall(
                agent_id="rag", tool_name="web_search", input={"query": "demo"}
            )
        ],
        justification="test",
    )

    await pipeline._dispatch_tool_calls(
        "rag", shared_ctx, fake_llm, fake_db_pool, fake_redis
    )

    # tool_calls row persisted exactly once for the single attempt.
    insert_calls = [
        (sql, args)
        for (sql, args) in fake_db_pool.executed
        if "INSERT INTO tool_calls" in sql
    ]
    assert len(insert_calls) == 1
    _sql, args = insert_calls[0]
    assert args[1] == "web_search"  # tool_name

    # agent_outputs bucket populated for the consumer agent.
    bucket = shared_ctx.agent_outputs.get("tools", {}).get("web_search")
    assert bucket and len(bucket) == 1
    assert bucket[0]["success"] is True
    assert bucket[0]["input"] == {"query": "demo"}

    # __tool_input__ key cleared after dispatch.
    assert "__tool_input__" not in shared_ctx.agent_outputs

    # tool_call_start + tool_call_end events both published.
    pushed = [
        cmd for (cmd, args) in fake_redis.commands if cmd == "rpush"
    ]
    assert len(pushed) >= 2  # at least the two SSE events


async def test_dispatch_skips_other_agents(
    shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    shared_ctx.routing_plan = RoutingPlan(
        agent_sequence=["rag", "synthesis"],
        tool_calls=[
            PlannedToolCall(
                agent_id="synthesis", tool_name="web_search", input={"query": "demo"}
            )
        ],
    )
    await pipeline._dispatch_tool_calls(
        "rag", shared_ctx, fake_llm, fake_db_pool, fake_redis
    )
    assert not fake_db_pool.executed
    assert "tools" not in shared_ctx.agent_outputs


async def test_dispatch_noop_when_no_routing_plan(
    shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    shared_ctx.routing_plan = None
    await pipeline._dispatch_tool_calls(
        "rag", shared_ctx, fake_llm, fake_db_pool, fake_redis
    )
    assert not fake_db_pool.executed


async def test_dispatch_noop_when_tool_calls_empty(
    shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    shared_ctx.routing_plan = RoutingPlan(
        agent_sequence=["rag", "synthesis"], tool_calls=[]
    )
    await pipeline._dispatch_tool_calls(
        "rag", shared_ctx, fake_llm, fake_db_pool, fake_redis
    )
    assert not fake_db_pool.executed


async def test_orchestrator_parses_tool_calls():
    """Back-compat: orchestrator handles missing/invalid tool_calls gracefully."""
    from app.agents.orchestrator import _parse_tool_calls

    # Valid entries pass through; invalid entries are filtered out.
    raw = [
        {"agent_id": "rag", "tool_name": "web_search", "input": {"q": "x"}},
        {"agent_id": "invalid_agent", "tool_name": "web_search", "input": {}},
        {"agent_id": "rag", "tool_name": "unknown_tool", "input": {}},
        {"agent_id": "synthesis", "tool_name": "code_exec", "input": {"code": "p"}},
        "not a dict",
    ]
    parsed = _parse_tool_calls(raw)
    assert len(parsed) == 2
    assert parsed[0].agent_id == "rag"
    assert parsed[0].tool_name == "web_search"
    assert parsed[1].tool_name == "code_exec"


async def test_orchestrator_parses_missing_tool_calls():
    from app.agents.orchestrator import _parse_tool_calls

    assert _parse_tool_calls(None) == []
    assert _parse_tool_calls([]) == []
    assert _parse_tool_calls("not a list") == []
