from __future__ import annotations

from pathlib import Path

import asyncpg

SQL_DIR = Path(__file__).parent / "sql"
INIT_SQL = SQL_DIR / "001_init.sql"


async def init_schema(pool: asyncpg.Pool) -> None:
    """Run 001_init.sql idempotently. The script uses IF NOT EXISTS guards."""
    sql = INIT_SQL.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(sql)
