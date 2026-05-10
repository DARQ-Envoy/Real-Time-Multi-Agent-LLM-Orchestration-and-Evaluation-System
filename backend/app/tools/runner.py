"""Tool execution runner with 1-retry policy and tool_calls persistence."""

from __future__ import annotations

import json
import uuid
from typing import Awaitable, Callable

from ..llm import LLMClient
from ..models import SharedContext, ToolCallEndEvent, ToolCallStartEvent, ToolResult
from ..persistence import sha256_json
from ..redis_bus import publish_event

ToolFn = Callable[[SharedContext, LLMClient], Awaitable[ToolResult]]

MAX_RETRIES_DEFAULT = 1


def _accept(result: ToolResult) -> bool:
    if not result.success:
        return False
    if result.data is None:
        return False
    if isinstance(result.data, (list, dict, str)) and not result.data:
        return False
    return True


async def run_with_retry(
    tool_fn: ToolFn,
    ctx: SharedContext,
    llm: LLMClient,
    db_pool,
    redis,
    tool_name: str,
    max_retries: int = MAX_RETRIES_DEFAULT,
) -> ToolResult:
    """Run `tool_fn` up to `max_retries+1` times. Persist each attempt and stream SSE.

    Stops on first accepted result. Returns the last attempted ToolResult.
    """
    job_uuid = uuid.UUID(ctx.job_id)
    input_payload = {"job_id": ctx.job_id, "query_hash": sha256_json(ctx.query)}
    input_hash = sha256_json(input_payload) or ""
    last: ToolResult | None = None

    for attempt in range(max_retries + 1):
        await publish_event(
            redis,
            ctx.job_id,
            ToolCallStartEvent(tool_name=tool_name, input_hash=input_hash).model_dump(),
        )
        result = await tool_fn(ctx, llm)
        result.tool_name = tool_name
        result.retry_number = attempt
        result.accepted_by_agent = _accept(result)
        last = result

        await publish_event(
            redis,
            ctx.job_id,
            ToolCallEndEvent(
                tool_name=tool_name,
                latency_ms=result.latency_ms,
                success=result.success,
            ).model_dump(),
        )

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tool_calls
                    (job_id, tool_name, input, output, latency_ms, success,
                     error_code, accepted, retry_number)
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, $9)
                """,
                job_uuid,
                tool_name,
                json.dumps(input_payload),
                json.dumps(result.data) if result.data is not None else None,
                result.latency_ms,
                result.success,
                result.error_code,
                result.accepted_by_agent,
                attempt,
            )

        if result.accepted_by_agent:
            return result

    assert last is not None
    return last
