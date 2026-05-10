from __future__ import annotations

import json
import time
import uuid
from typing import Any

from arq.connections import RedisSettings

from . import pipeline
from .db import create_pool
from .models import ErrorEvent, JobCompleteEvent, SharedContext
from .redis_bus import publish_event
from .settings import settings


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.REDIS_URL)


async def startup(ctx: dict[str, Any]) -> None:
    ctx["db_pool"] = await create_pool()


async def shutdown(ctx: dict[str, Any]) -> None:
    pool = ctx.get("db_pool")
    if pool is not None:
        await pool.close()


async def run_query(ctx: dict[str, Any], job_id: str, query: str) -> dict[str, Any]:
    db_pool = ctx["db_pool"]
    redis = ctx["redis"]
    started = time.perf_counter()
    job_uuid = uuid.UUID(job_id)

    async with db_pool.acquire() as conn:
        await conn.execute(
            "UPDATE jobs SET status = $1 WHERE id = $2", "RUNNING", job_uuid
        )

    shared = SharedContext(job_id=job_id, query=query)
    final_status = "FAILED"
    final_answer: list[dict[str, Any]] = []

    try:
        provenance = await pipeline.run(shared, redis, db_pool)
        if provenance:
            final_answer = [p.model_dump() for p in provenance]
            final_status = "COMPLETE"
        else:
            final_status = "FAILED"
    except Exception as exc:
        await publish_event(
            redis,
            job_id,
            ErrorEvent(
                error_code="PIPELINE_FAILURE",
                message=str(exc),
                job_id=job_id,
            ).model_dump(),
        )
        final_status = "FAILED"

    async with db_pool.acquire() as conn:
        if final_answer:
            await conn.execute(
                """
                UPDATE jobs
                   SET status = $1,
                       completed_at = now(),
                       final_answer = $2::jsonb,
                       routing_plan = $3::jsonb
                 WHERE id = $4
                """,
                final_status,
                json.dumps(final_answer),
                json.dumps(shared.routing_plan.model_dump()) if shared.routing_plan else None,
                job_uuid,
            )
        else:
            await conn.execute(
                """
                UPDATE jobs
                   SET status = $1,
                       completed_at = now(),
                       routing_plan = $2::jsonb
                 WHERE id = $3
                """,
                final_status,
                json.dumps(shared.routing_plan.model_dump()) if shared.routing_plan else None,
                job_uuid,
            )

    total_latency_ms = (time.perf_counter() - started) * 1000.0
    await publish_event(
        redis,
        job_id,
        JobCompleteEvent(
            job_id=job_id, total_latency_ms=total_latency_ms
        ).model_dump(),
    )
    return {"job_id": job_id, "status": final_status}


class WorkerSettings:
    functions = [run_query]
    redis_settings = _redis_settings()
    on_startup = startup
    on_shutdown = shutdown
    keep_result = 60
    max_jobs = 4


__all__ = ["WorkerSettings", "run_query"]
