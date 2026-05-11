from __future__ import annotations

from pathlib import Path

import asyncpg

from .settings import settings

SQL_DIR = Path(__file__).parent / "sql"
INIT_SQL = SQL_DIR / "001_init.sql"
RO_GRANTS_SQL = SQL_DIR / "003_readonly_role.sql"


def _quote_pg_literal(value: str) -> str:
    """Escape a value for use as a Postgres string literal.

    Single quotes are doubled. The result MUST be embedded only inside a
    plain SQL string literal (i.e. not inside a `$$ ... $$` PL/pgSQL block),
    where dollar-sign sequences in the value cannot terminate any quoting
    context.
    """
    return "'" + value.replace("'", "''") + "'"


async def _ensure_readonly_role(conn: asyncpg.Connection, password: str) -> None:
    exists = await conn.fetchval(
        "SELECT 1 FROM pg_roles WHERE rolname = 'mega_ro'"
    )
    literal = _quote_pg_literal(password)
    if exists:
        await conn.execute(f"ALTER ROLE mega_ro WITH LOGIN PASSWORD {literal}")
    else:
        await conn.execute(f"CREATE ROLE mega_ro LOGIN PASSWORD {literal}")


async def init_schema(pool: asyncpg.Pool) -> None:
    """Apply schema migrations idempotently.

    Order:
      1. 001_init.sql — tables.
      2. mega_ro role (CREATE or ALTER) via parameter-safe Python branch.
      3. 003_readonly_role.sql — grants (idempotent re-application).
    """
    init_sql = INIT_SQL.read_text(encoding="utf-8")
    grants_sql = RO_GRANTS_SQL.read_text(encoding="utf-8")
    async with pool.acquire() as conn:
        await conn.execute(init_sql)
        await _ensure_readonly_role(conn, settings.MEGA_RO_PASSWORD)
        await conn.execute(grants_sql)
