"""2-retry FSM with per-row retry_reason and per-tool fallback dispatch."""

from __future__ import annotations

import json
import logging
import time
import uuid
from typing import Any, Awaitable, Callable

from ..llm import LLMClient
from ..models import SharedContext, ToolCallEndEvent, ToolCallStartEvent, ToolResult
from ..persistence import sha256_json
from ..redis_bus import publish_event

ToolFn = Callable[[SharedContext, LLMClient], Awaitable[ToolResult]]

MAX_RETRIES_DEFAULT = 2

_log = logging.getLogger(__name__)

# Closed retry_reason vocabulary. Anything outside this set degrades to
# "not_accepted" rather than crashing under `python -O` (assert-stripped).
_RETRY_REASONS = {
    "timeout",
    "empty_result",
    "malformed",
    "exec_error",
    "not_accepted",
    "malformed_schema_hint_injected",
}

_ERROR_TO_REASON = {
    "TIMEOUT": "timeout",
    "EMPTY": "empty_result",
    "MALFORMED": "malformed",
    "EXEC_ERROR": "exec_error",
}


def _accept(result: ToolResult) -> bool:
    if not result.success:
        return False
    if result.data is None:
        return False
    if isinstance(result.data, (list, dict, str)) and not result.data:
        return False
    return True


def _normalize_retry_reason(prior: ToolResult) -> str:
    if prior.error_code and prior.error_code in _ERROR_TO_REASON:
        return _ERROR_TO_REASON[prior.error_code]
    return "not_accepted"


async def _safe_publish(redis, job_id: str, event: dict[str, Any]) -> None:
    try:
        await publish_event(redis, job_id, event)
    except Exception as exc:
        _log.warning(
            "publish_event failed for job=%s type=%s: %s",
            job_id,
            event.get("type"),
            exc,
        )


async def _safe_insert_row(db_pool, *args) -> None:
    try:
        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO tool_calls
                    (job_id, tool_name, input, output, latency_ms, success,
                     error_code, accepted, retry_number, retry_reason)
                VALUES ($1, $2, $3::jsonb, $4::jsonb, $5, $6, $7, $8, $9, $10)
                """,
                *args,
            )
    except Exception as exc:
        _log.warning("tool_calls INSERT failed: %s", exc)


async def run_with_retry(
    tool_fn: ToolFn,
    ctx: SharedContext,
    llm: LLMClient,
    db_pool,
    redis,
    tool_name: str,
    max_retries: int = MAX_RETRIES_DEFAULT,
    input_payload: dict[str, Any] | None = None,
) -> ToolResult:
    """Run `tool_fn` up to `max_retries+1` times.

    Persist each attempt with `retry_number` and `retry_reason`. The first
    attempt's retry_reason is NULL. On chain exhaustion, dispatch the
    registered fallback for `(tool_name, terminal_error_code)`. The fallback
    fires at the end of EVERY exhausted chain (including `max_retries=0` for
    self_reflection's single-attempt advisory path) — the registry decides
    whether anything actually runs by keying on `(tool_name, error_code)`.

    Exceptions raised by `tool_fn`, `publish_event`, or the DB INSERT do not
    propagate: tool_fn exceptions become a synthetic `ToolResult(EXEC_ERROR)`
    and continue the retry loop; infra (Redis/DB) failures log and continue
    so the FSM can finish.
    """
    job_uuid = uuid.UUID(ctx.job_id)
    persisted_payload = input_payload if input_payload is not None else {
        "job_id": ctx.job_id,
        "query_hash": sha256_json(ctx.query),
    }
    input_hash = sha256_json(persisted_payload) or ""
    payload_json = json.dumps(persisted_payload)
    last: ToolResult | None = None
    next_retry_reason: str | None = None

    for attempt in range(max_retries + 1):
        await _safe_publish(
            redis,
            ctx.job_id,
            ToolCallStartEvent(tool_name=tool_name, input_hash=input_hash).model_dump(),
        )
        try:
            result = await tool_fn(ctx, llm)
        except Exception as exc:
            result = ToolResult(
                tool_name=tool_name,
                success=False,
                data=None,
                error_code="EXEC_ERROR",
                error_message=f"{exc.__class__.__name__}: {exc}",
                latency_ms=0.0,
            )
        result.tool_name = tool_name
        result.retry_number = attempt
        result.accepted_by_agent = _accept(result)
        last = result

        await _safe_publish(
            redis,
            ctx.job_id,
            ToolCallEndEvent(
                tool_name=tool_name,
                latency_ms=result.latency_ms,
                success=result.success,
            ).model_dump(),
        )

        row_reason = next_retry_reason if attempt > 0 else None
        await _safe_insert_row(
            db_pool,
            job_uuid,
            tool_name,
            payload_json,
            json.dumps(result.data) if result.data is not None else None,
            result.latency_ms,
            result.success,
            result.error_code,
            result.accepted_by_agent,
            attempt,
            row_reason,
        )

        if result.accepted_by_agent:
            ctx.agent_outputs.pop("__retry_hint__", None)
            return result

        if attempt < max_retries:
            hint_active = ctx.agent_outputs.get("__retry_hint__") == "schema_simplified"
            if (
                tool_name == "sql_lookup"
                and result.error_code == "MALFORMED"
                and not hint_active
            ):
                ctx.agent_outputs["__retry_hint__"] = "schema_simplified"
                next_retry_reason = "malformed_schema_hint_injected"
            elif hint_active and tool_name == "sql_lookup":
                # Hint is still being applied — keep the audit honest.
                next_retry_reason = "malformed_schema_hint_injected"
            else:
                next_retry_reason = _normalize_retry_reason(result)
            if next_retry_reason not in _RETRY_REASONS:
                next_retry_reason = "not_accepted"
            continue

    # Chain exhausted. Dispatch the registered fallback (if any). The
    # registry is keyed by (tool_name, error_code) — unregistered combinations
    # are silent no-ops.
    ctx.agent_outputs.pop("__retry_hint__", None)
    assert last is not None
    from .fallbacks import maybe_dispatch
    await maybe_dispatch(tool_name, last, ctx, llm, db_pool, redis)
    return last


__all__ = ["run_with_retry", "ToolFn", "MAX_RETRIES_DEFAULT"]
