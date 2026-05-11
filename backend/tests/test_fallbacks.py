"""Per-fallback unit tests — each callable's side effects in isolation."""

from __future__ import annotations

import json

import pytest

from app.models import ToolResult
from app.tools import fallbacks as fallbacks_mod


def _terminal(code: str, msg: str = "boom") -> ToolResult:
    return ToolResult(
        tool_name="x",
        success=False,
        data=None,
        error_code=code,
        error_message=msg,
        latency_ms=1.0,
    )


def _published_error_codes(redis) -> list[str]:
    """Extract `error_code` strings from every rpushed payload in the fake Redis."""
    codes: list[str] = []
    for cmd, args in redis.commands:
        if cmd != "rpush":
            continue
        # args = (key, payload) per redis_bus.publish_event
        payload = args[1] if len(args) > 1 else args[0]
        try:
            event = json.loads(payload)
        except Exception:
            continue
        if event.get("type") == "error" and event.get("error_code"):
            codes.append(event["error_code"])
    return codes


async def test_web_search_timeout_marks_unavailable_and_emits_event(
    monkeypatch, shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    # Stub the nested run_with_retry → self_reflection so the test stays unit-level.
    from app.tools import retry as retry_mod

    async def stub_run_with_retry(tool_fn, ctx, llm, db_pool, redis, **kw):
        return None

    monkeypatch.setattr(retry_mod, "run_with_retry", stub_run_with_retry)

    await fallbacks_mod.maybe_dispatch(
        "web_search", _terminal("TIMEOUT"), shared_ctx, fake_llm, fake_db_pool, fake_redis
    )
    assert shared_ctx.agent_outputs.get("web_unavailable") is True
    assert "WEB_FALLBACK" in _published_error_codes(fake_redis)


async def test_code_exec_failure_sets_flag(shared_ctx, fake_llm, fake_db_pool, fake_redis):
    await fallbacks_mod.maybe_dispatch(
        "code_exec",
        _terminal("EXEC_ERROR", "boom"),
        shared_ctx,
        fake_llm,
        fake_db_pool,
        fake_redis,
    )
    flag = shared_ctx.agent_outputs.get("code_exec_failed")
    assert flag and flag["suggested_replan"] is True
    assert flag["reason"] == "boom"
    assert "TOOL_FAILURE" in _published_error_codes(fake_redis)


async def test_sql_lookup_malformed_emits_skipped(
    shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    await fallbacks_mod.maybe_dispatch(
        "sql_lookup", _terminal("MALFORMED"), shared_ctx, fake_llm, fake_db_pool, fake_redis
    )
    assert "SQL_FALLBACK_SKIPPED" in _published_error_codes(fake_redis)
    # No ctx mutation specified for this fallback.
    assert "web_unavailable" not in shared_ctx.agent_outputs


async def test_self_reflection_failed_emits_event(
    shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    await fallbacks_mod.maybe_dispatch(
        "self_reflection",
        _terminal("EXEC_ERROR"),
        shared_ctx,
        fake_llm,
        fake_db_pool,
        fake_redis,
    )
    assert "SELF_REFLECTION_FAILED" in _published_error_codes(fake_redis)


async def test_unknown_combination_is_noop(
    shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    await fallbacks_mod.maybe_dispatch(
        "web_search",
        _terminal("EMPTY"),  # no fallback registered for EMPTY
        shared_ctx,
        fake_llm,
        fake_db_pool,
        fake_redis,
    )
    assert _published_error_codes(fake_redis) == []


async def test_fallback_exception_emits_fallback_failure(
    monkeypatch, shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    async def boom(ctx, llm, db_pool, redis, terminal):
        raise RuntimeError("kaboom")

    monkeypatch.setitem(
        fallbacks_mod.FALLBACK_REGISTRY, ("web_search", "TIMEOUT"), boom
    )
    await fallbacks_mod.maybe_dispatch(
        "web_search", _terminal("TIMEOUT"), shared_ctx, fake_llm, fake_db_pool, fake_redis
    )
    codes = _published_error_codes(fake_redis)
    assert "FALLBACK_FAILURE" in codes
