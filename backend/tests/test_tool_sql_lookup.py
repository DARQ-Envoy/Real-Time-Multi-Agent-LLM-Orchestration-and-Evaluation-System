"""sql_lookup integration tests — require a running Postgres + mega_ro role.

Skipped automatically if the read-only pool cannot be opened (e.g. the docker
stack is not up). Run via `docker compose exec api pytest tests/test_tool_sql_lookup.py`.
"""

from __future__ import annotations

import pytest

from app.db import create_ro_pool
from app.settings import settings
from app.tools import sql_lookup


@pytest.fixture
async def ro_pool():
    try:
        pool = await create_ro_pool()
    except Exception as exc:  # pragma: no cover - env-dependent
        pytest.skip(f"mega_ro pool unavailable: {exc}")
    sql_lookup.set_ro_pool(pool)
    try:
        yield pool
    finally:
        sql_lookup.set_ro_pool(None)
        await pool.close()


async def test_malformed_non_select(ro_pool, shared_ctx, fake_llm):
    shared_ctx.agent_outputs["__tool_input__"] = {"sql": "DROP TABLE jobs;"}
    result = await sql_lookup.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "MALFORMED"


async def test_happy_path(ro_pool, shared_ctx, fake_llm):
    shared_ctx.agent_outputs["__tool_input__"] = {
        "sql": "SELECT 1 AS one LIMIT 1"
    }
    result = await sql_lookup.run(shared_ctx, fake_llm)
    assert result.success is True
    assert result.data["columns"] == ["one"]
    assert result.data["rows"] == [[1]]


async def test_timeout(monkeypatch, ro_pool, shared_ctx, fake_llm):
    monkeypatch.setattr(settings, "SQL_LOOKUP_TIMEOUT_SECONDS", 1)
    shared_ctx.agent_outputs["__tool_input__"] = {
        "sql": (
            "SELECT count(*) FROM agent_logs a, agent_logs b, agent_logs c "
            "WHERE pg_sleep(2) IS NULL LIMIT 1"
        )
    }
    result = await sql_lookup.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code in {"TIMEOUT", "EXEC_ERROR"}


async def test_exec_error_bad_column(ro_pool, shared_ctx, fake_llm):
    shared_ctx.agent_outputs["__tool_input__"] = {
        "sql": "SELECT nonexistent_column FROM jobs LIMIT 1"
    }
    result = await sql_lookup.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "EXEC_ERROR"


async def test_missing_pool_returns_malformed(shared_ctx, fake_llm):
    sql_lookup.set_ro_pool(None)
    shared_ctx.agent_outputs["__tool_input__"] = {"sql": "SELECT 1 LIMIT 1"}
    result = await sql_lookup.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "MALFORMED"


async def test_ro_role_cannot_insert(ro_pool):
    """Defense-in-depth: even if SELECT-check were bypassed, role lacks write privilege."""
    import asyncpg

    async with ro_pool.acquire() as conn:
        with pytest.raises(asyncpg.exceptions.InsufficientPrivilegeError):
            await conn.execute(
                "INSERT INTO jobs(id, query, status) VALUES (gen_random_uuid(), 'x', 'QUEUED')"
            )
