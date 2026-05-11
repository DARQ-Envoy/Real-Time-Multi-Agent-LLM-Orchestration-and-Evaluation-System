"""Per-tool fallback contracts per README §Tool Catalogue."""

from __future__ import annotations

from typing import Awaitable, Callable

from ..llm import LLMClient
from ..models import ErrorEvent, SharedContext, ToolResult
from ..redis_bus import publish_event

FallbackFn = Callable[
    [SharedContext, LLMClient, object, object, ToolResult],
    Awaitable[None],
]


async def _emit_error(redis, job_id: str, code: str, message: str) -> None:
    await publish_event(
        redis,
        job_id,
        ErrorEvent(error_code=code, message=message, job_id=job_id).model_dump(),
    )


async def _web_search_timeout(
    ctx: SharedContext,
    llm: LLMClient,
    db_pool,
    redis,
    terminal: ToolResult,
) -> None:
    # Re-use the existing self_reflection tool through the same retry path,
    # but advisory (no retries). The tool_calls row is recorded normally.
    from . import self_reflection
    from .retry import run_with_retry

    ctx.agent_outputs["web_unavailable"] = True
    await _emit_error(
        redis,
        ctx.job_id,
        "WEB_FALLBACK",
        "web_search exhausted retries; falling back to self_reflection",
    )
    await run_with_retry(
        self_reflection.run,
        ctx,
        llm,
        db_pool,
        redis,
        tool_name="self_reflection",
        max_retries=0,
    )


async def _code_exec_failure(
    ctx: SharedContext,
    llm: LLMClient,
    db_pool,
    redis,
    terminal: ToolResult,
) -> None:
    ctx.agent_outputs["code_exec_failed"] = {
        "reason": terminal.error_message or "EXEC_ERROR",
        "suggested_replan": True,
    }
    await _emit_error(
        redis,
        ctx.job_id,
        "TOOL_FAILURE",
        "code_exec exhausted retries; decomposition should avoid code paths",
    )


async def _sql_lookup_malformed(
    ctx: SharedContext,
    llm: LLMClient,
    db_pool,
    redis,
    terminal: ToolResult,
) -> None:
    await _emit_error(
        redis,
        ctx.job_id,
        "SQL_FALLBACK_SKIPPED",
        "sql_lookup malformed after retries with simplified-schema hint; skipping",
    )


async def _self_reflection_failed(
    ctx: SharedContext,
    llm: LLMClient,
    db_pool,
    redis,
    terminal: ToolResult,
) -> None:
    await _emit_error(
        redis,
        ctx.job_id,
        "SELF_REFLECTION_FAILED",
        "self_reflection raised; advisory, continuing",
    )


FALLBACK_REGISTRY: dict[tuple[str, str], FallbackFn] = {
    ("web_search", "TIMEOUT"): _web_search_timeout,
    ("code_exec", "EXEC_ERROR"): _code_exec_failure,
    ("sql_lookup", "MALFORMED"): _sql_lookup_malformed,
    ("self_reflection", "EXEC_ERROR"): _self_reflection_failed,
}


async def maybe_dispatch(
    tool_name: str,
    terminal: ToolResult,
    ctx: SharedContext,
    llm: LLMClient,
    db_pool,
    redis,
) -> None:
    """Look up `(tool_name, terminal.error_code)` and run the fallback if registered.

    Exceptions inside the fallback are swallowed and reported as `FALLBACK_FAILURE`.
    """
    key = (tool_name, terminal.error_code or "")
    fn = FALLBACK_REGISTRY.get(key)
    if fn is None:
        return
    try:
        await fn(ctx, llm, db_pool, redis, terminal)
    except Exception as exc:
        await _emit_error(
            redis,
            ctx.job_id,
            "FALLBACK_FAILURE",
            f"{tool_name} fallback raised: {exc.__class__.__name__}: {exc}",
        )


__all__ = ["FALLBACK_REGISTRY", "maybe_dispatch", "FallbackFn"]
