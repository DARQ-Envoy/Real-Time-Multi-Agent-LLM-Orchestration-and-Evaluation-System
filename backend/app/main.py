from __future__ import annotations

import json
import uuid
from contextlib import asynccontextmanager

from pathlib import Path

from arq import create_pool as create_arq_pool
from arq.connections import RedisSettings
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import bootstrap
from .db import create_pool as create_db_pool
from .models import JobRequest, JobResponse
from .redis_bus import subscribe
from .settings import settings

STATIC_DIR = Path(__file__).parent / "static"


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.REDIS_URL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db_pool = await create_db_pool()
    await bootstrap.init_schema(db_pool)
    arq_pool = await create_arq_pool(_redis_settings())
    app.state.db_pool = db_pool
    app.state.arq = arq_pool
    try:
        yield
    finally:
        await arq_pool.aclose()
        await db_pool.close()


app = FastAPI(title="Mega AI", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/", include_in_schema=False)
async def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/healthz")
async def healthz(request: Request):
    body = {"status": "ok", "db": "ok", "redis": "ok"}
    status_code = 200
    try:
        async with request.app.state.db_pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
    except Exception as exc:
        body["db"] = f"error: {exc.__class__.__name__}"
        body["status"] = "degraded"
        status_code = 503
    try:
        await request.app.state.arq.ping()
    except Exception as exc:
        body["redis"] = f"error: {exc.__class__.__name__}"
        body["status"] = "degraded"
        status_code = 503
    return JSONResponse(body, status_code=status_code)


@app.post("/query", response_model=JobResponse, status_code=202)
async def submit_query(req: JobRequest, request: Request):
    if req.max_budget_tokens > settings.MAX_BUDGET_TOKENS:
        raise HTTPException(
            status_code=400,
            detail={
                "error_code": "BUDGET_EXCEEDED",
                "message": "Requested budget exceeds system maximum",
            },
        )
    job_id = uuid.uuid4()
    async with request.app.state.db_pool.acquire() as conn:
        await conn.execute(
            "INSERT INTO jobs (id, query, status) VALUES ($1, $2, $3)",
            job_id,
            req.query,
            "QUEUED",
        )
    enqueued = await request.app.state.arq.enqueue_job(
        "run_query", str(job_id), req.query
    )
    if enqueued is None:
        raise HTTPException(
            status_code=503,
            detail={"error_code": "ENQUEUE_FAILED", "message": "Worker unavailable"},
        )
    return JobResponse(job_id=str(job_id), stream_url=f"/stream/{job_id}")


@app.get("/stream/{job_id}")
async def stream(job_id: str, request: Request):
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail={"error_code": "JOB_NOT_FOUND"})
    async with request.app.state.db_pool.acquire() as conn:
        row = await conn.fetchrow("SELECT id FROM jobs WHERE id = $1", job_uuid)
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "JOB_NOT_FOUND"})

    redis = request.app.state.arq

    async def gen():
        async for event in subscribe(redis, job_id):
            yield f"data: {json.dumps(event)}\n\n"

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@app.get("/trace/{job_id}")
async def trace(job_id: str, request: Request):
    try:
        job_uuid = uuid.UUID(job_id)
    except ValueError:
        raise HTTPException(status_code=404, detail={"error_code": "JOB_NOT_FOUND"})
    async with request.app.state.db_pool.acquire() as conn:
        row = await conn.fetchrow(
            """
            SELECT id, status, final_answer, routing_plan
              FROM jobs
             WHERE id = $1
            """,
            job_uuid,
        )
    if row is None:
        raise HTTPException(status_code=404, detail={"error_code": "JOB_NOT_FOUND"})
    final_answer = json.loads(row["final_answer"]) if row["final_answer"] else None
    routing_plan = json.loads(row["routing_plan"]) if row["routing_plan"] else None
    return {
        "job_id": str(row["id"]),
        "status": row["status"],
        "final_answer": final_answer,
        "routing_plan": routing_plan,
    }
