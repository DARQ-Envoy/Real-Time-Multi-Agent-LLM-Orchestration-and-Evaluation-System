"""E2 retry FSM tests — drives crafted ToolResult sequences through retry.run_with_retry.

Verifies row counts, retry_reason vocabulary, fallback dispatch, and idempotency.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.models import ToolResult
from app.tools import retry as retry_mod


def _ok(data: Any = "x") -> ToolResult:
    return ToolResult(tool_name="x", success=True, data=data, latency_ms=1.0)


def _fail(code: str, msg: str = "boom") -> ToolResult:
    return ToolResult(
        tool_name="x",
        success=False,
        data=None,
        error_code=code,
        error_message=msg,
        latency_ms=1.0,
    )


def _make_fn(results: list[ToolResult]):
    cursor = {"i": 0}

    async def fn(ctx, llm):
        i = cursor["i"]
        cursor["i"] = i + 1
        return results[i] if i < len(results) else _fail("EXEC_ERROR", "exhausted")

    return fn


def _insert_rows(pool) -> list[tuple]:
    return [
        args
        for (sql, args) in pool.executed
        if "INSERT INTO tool_calls" in sql
    ]


def _row_field(row_args: tuple, name: str):
    # Column ordering in retry.py INSERT:
    # (job_id, tool_name, input, output, latency_ms, success, error_code,
    #  accepted, retry_number, retry_reason)
    fields = [
        "job_id",
        "tool_name",
        "input",
        "output",
        "latency_ms",
        "success",
        "error_code",
        "accepted",
        "retry_number",
        "retry_reason",
    ]
    return row_args[fields.index(name)]


async def test_happy_attempt_0_no_retry(shared_ctx, fake_llm, fake_db_pool, fake_redis):
    fn = _make_fn([_ok()])
    result = await retry_mod.run_with_retry(
        fn, shared_ctx, fake_llm, fake_db_pool, fake_redis, tool_name="web_search"
    )
    rows = _insert_rows(fake_db_pool)
    assert result.success is True
    assert len(rows) == 1
    assert _row_field(rows[0], "retry_number") == 0
    assert _row_field(rows[0], "retry_reason") is None


async def test_1_retry_recovery(shared_ctx, fake_llm, fake_db_pool, fake_redis):
    fn = _make_fn([_fail("TIMEOUT"), _ok()])
    result = await retry_mod.run_with_retry(
        fn, shared_ctx, fake_llm, fake_db_pool, fake_redis, tool_name="web_search"
    )
    rows = _insert_rows(fake_db_pool)
    assert result.success is True
    assert len(rows) == 2
    assert _row_field(rows[0], "retry_reason") is None
    assert _row_field(rows[1], "retry_reason") == "timeout"
    assert _row_field(rows[1], "retry_number") == 1


async def test_web_search_2_retry_exhaustion_triggers_fallback(
    monkeypatch, shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    fired = {"web_unavailable": False, "events": []}

    async def fake_self_reflection(ctx, llm):
        return _ok({"contradictions": []})

    async def stub_fallback(ctx, llm, db_pool, redis, terminal):
        # Mirror the production fallback's user-visible effects.
        ctx.agent_outputs["web_unavailable"] = True
        from app.models import ErrorEvent
        from app.redis_bus import publish_event

        await publish_event(
            redis,
            ctx.job_id,
            ErrorEvent(
                error_code="WEB_FALLBACK", message="x", job_id=ctx.job_id
            ).model_dump(),
        )

    from app.tools import fallbacks as fallbacks_mod

    monkeypatch.setitem(
        fallbacks_mod.FALLBACK_REGISTRY, ("web_search", "TIMEOUT"), stub_fallback
    )

    fn = _make_fn([_fail("TIMEOUT"), _fail("TIMEOUT"), _fail("TIMEOUT")])
    result = await retry_mod.run_with_retry(
        fn, shared_ctx, fake_llm, fake_db_pool, fake_redis, tool_name="web_search"
    )

    rows = _insert_rows(fake_db_pool)
    assert len(rows) == 3
    assert _row_field(rows[0], "retry_reason") is None
    assert _row_field(rows[1], "retry_reason") == "timeout"
    assert _row_field(rows[2], "retry_reason") == "timeout"
    assert result.success is False
    assert result.error_code == "TIMEOUT"
    assert shared_ctx.agent_outputs.get("web_unavailable") is True


async def test_sql_lookup_hint_injected_at_retry_1(
    shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    seen_hints: list[str | None] = []

    async def fn(ctx, llm):
        seen_hints.append(ctx.agent_outputs.get("__retry_hint__"))
        if len(seen_hints) == 1:
            return _fail("MALFORMED", "bad sql")
        return _ok({"columns": ["id"], "rows": [[1]], "sql": "SELECT id FROM jobs LIMIT 1"})

    result = await retry_mod.run_with_retry(
        fn, shared_ctx, fake_llm, fake_db_pool, fake_redis, tool_name="sql_lookup"
    )
    assert result.success is True
    rows = _insert_rows(fake_db_pool)
    assert len(rows) == 2
    assert _row_field(rows[0], "retry_reason") is None
    assert _row_field(rows[1], "retry_reason") == "malformed_schema_hint_injected"
    # Hint was absent on attempt 0 and present on attempt 1.
    assert seen_hints == [None, "schema_simplified"]
    # Hint cleared on success.
    assert "__retry_hint__" not in shared_ctx.agent_outputs


async def test_sql_lookup_malformed_through_retry_2(
    monkeypatch, shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    skipped: dict[str, bool] = {"called": False}

    async def stub_skip(ctx, llm, db_pool, redis, terminal):
        skipped["called"] = True

    from app.tools import fallbacks as fallbacks_mod

    monkeypatch.setitem(
        fallbacks_mod.FALLBACK_REGISTRY, ("sql_lookup", "MALFORMED"), stub_skip
    )

    fn = _make_fn([_fail("MALFORMED"), _fail("MALFORMED"), _fail("MALFORMED")])
    await retry_mod.run_with_retry(
        fn, shared_ctx, fake_llm, fake_db_pool, fake_redis, tool_name="sql_lookup"
    )
    rows = _insert_rows(fake_db_pool)
    assert len(rows) == 3
    assert skipped["called"] is True


async def test_self_reflection_max_retries_0_no_retry(
    shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    fn = _make_fn([_fail("EXEC_ERROR")])
    result = await retry_mod.run_with_retry(
        fn,
        shared_ctx,
        fake_llm,
        fake_db_pool,
        fake_redis,
        tool_name="self_reflection",
        max_retries=0,
    )
    rows = _insert_rows(fake_db_pool)
    assert len(rows) == 1
    assert result.success is False


async def test_no_retry_when_first_attempt_succeeds(
    shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    """Sanity: success on attempt 0 → no second attempt invoked."""

    cursor = {"i": 0}

    async def fn(ctx, llm):
        cursor["i"] += 1
        return _ok([1, 2, 3])

    result = await retry_mod.run_with_retry(
        fn, shared_ctx, fake_llm, fake_db_pool, fake_redis, tool_name="web_search"
    )
    assert result.success is True
    assert cursor["i"] == 1


async def test_chain_idempotency_no_extra_retries_after_fallback(
    monkeypatch, shared_ctx, fake_llm, fake_db_pool, fake_redis
):
    """AC4: after fallback, no further retries on the same chain. Second call to
    run_with_retry that succeeds on attempt 0 must NOT re-fire the fallback."""
    fired = {"count": 0}

    async def stub_fallback(ctx, llm, db_pool, redis, terminal):
        fired["count"] += 1

    from app.tools import fallbacks as fallbacks_mod

    monkeypatch.setitem(
        fallbacks_mod.FALLBACK_REGISTRY, ("web_search", "TIMEOUT"), stub_fallback
    )

    fn1 = _make_fn([_fail("TIMEOUT"), _fail("TIMEOUT"), _fail("TIMEOUT")])
    await retry_mod.run_with_retry(
        fn1, shared_ctx, fake_llm, fake_db_pool, fake_redis, tool_name="web_search"
    )
    assert fired["count"] == 1

    fn2 = _make_fn([_ok()])
    await retry_mod.run_with_retry(
        fn2, shared_ctx, fake_llm, fake_db_pool, fake_redis, tool_name="web_search"
    )
    assert fired["count"] == 1  # unchanged — no second fallback
