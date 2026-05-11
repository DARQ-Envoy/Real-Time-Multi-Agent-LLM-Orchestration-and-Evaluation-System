"""sql_lookup tool: NL -> SELECT against a read-only Postgres role."""

from __future__ import annotations

import base64
import datetime as _dt
import decimal
import re
import time
import uuid
from typing import Any

import asyncpg

from ..llm import LLMClient
from ..models import SharedContext, ToolResult
from ..settings import settings

EMIT_SQL_TOOL: dict[str, Any] = {
    "name": "emit_sql",
    "description": (
        "Emit ONE read-only SQL SELECT statement that answers the user's question "
        "using the provided schema. Never emit DML/DDL. Always include a LIMIT."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "sql": {"type": "string"},
            "justification": {"type": "string"},
        },
        "required": ["sql", "justification"],
    },
}

_SYSTEM_PROMPT = (
    "You translate a natural-language question into one read-only Postgres SELECT "
    "statement that answers it using ONLY the columns in the provided schema. "
    "Always call emit_sql. Never DROP, INSERT, UPDATE, DELETE, ALTER, GRANT, REVOKE, "
    "or CREATE. Always end the SELECT with an explicit LIMIT (<= 100)."
)

_ro_pool: asyncpg.Pool | None = None
_schema_cache: str | None = None
_compact_schema_cache: str | None = None
_COMMENT_LINE = re.compile(r"--[^\n]*")
_COMMENT_BLOCK = re.compile(r"/\*.*?\*/", re.DOTALL)


def set_ro_pool(pool: asyncpg.Pool | None) -> None:
    global _ro_pool, _schema_cache, _compact_schema_cache
    _ro_pool = pool
    if pool is None:
        _schema_cache = None
        _compact_schema_cache = None


async def _load_schema_text(pool: asyncpg.Pool) -> str:
    global _schema_cache
    if _schema_cache is not None:
        return _schema_cache
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT table_name, column_name, data_type
              FROM information_schema.columns
             WHERE table_schema = 'public'
             ORDER BY table_name, ordinal_position
            """
        )
    grouped: dict[str, list[str]] = {}
    for r in rows:
        grouped.setdefault(r["table_name"], []).append(
            f"{r['column_name']} {r['data_type']}"
        )
    if not grouped:
        _schema_cache = "(empty public schema)"
        return _schema_cache
    lines = [
        f"{table}({', '.join(cols)})" for table, cols in sorted(grouped.items())
    ]
    _schema_cache = "\n".join(lines)
    return _schema_cache


async def _load_compact_schema_text(pool: asyncpg.Pool) -> str:
    global _compact_schema_cache
    if _compact_schema_cache is not None:
        return _compact_schema_cache
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            """
            SELECT DISTINCT table_name
              FROM information_schema.columns
             WHERE table_schema = 'public'
             ORDER BY table_name
            """
        )
    names = [r["table_name"] for r in rows]
    _compact_schema_cache = (
        ", ".join(names) if names else "(empty public schema)"
    )
    return _compact_schema_cache


def _strip_comments(sql: str) -> str:
    s = _COMMENT_BLOCK.sub(" ", sql)
    s = _COMMENT_LINE.sub(" ", s)
    return s.strip()


def _is_select_only(sql: str) -> bool:
    stripped = _strip_comments(sql).lstrip("(").lstrip()
    if not stripped:
        return False
    head = stripped[:6].upper()
    if head != "SELECT" and not stripped[:4].upper() == "WITH":
        return False
    forbidden = ("INSERT", "UPDATE", "DELETE", "DROP", "ALTER", "GRANT", "REVOKE", "CREATE", "TRUNCATE", "MERGE")
    upper = stripped.upper()
    return not any(re.search(rf"\b{kw}\b", upper) for kw in forbidden)


def _json_safe(value: Any) -> Any:
    """Coerce asyncpg value types into JSON-serializable equivalents.

    Without this, json.dumps in tools/runner.py crashes on common Postgres
    types (UUID, datetime, Decimal, bytes), which would propagate and mark
    the whole job FAILED.
    """
    if value is None:
        return None
    if isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, _dt.timedelta):
        return value.total_seconds()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(value)).decode("ascii")
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    return str(value)


async def _execute(pool: asyncpg.Pool, sql: str) -> tuple[list[str], list[list[Any]]]:
    timeout_ms = settings.SQL_LOOKUP_TIMEOUT_SECONDS * 1000
    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.execute(f"SET LOCAL statement_timeout = {timeout_ms}")
            records = await conn.fetch(sql)
    if not records:
        return [], []
    columns = list(records[0].keys())
    rows = [[_json_safe(record[c]) for c in columns] for record in records]
    return columns, rows


async def run(ctx: SharedContext, llm: LLMClient) -> ToolResult:
    started = time.perf_counter()
    pool = _ro_pool
    if pool is None:
        return ToolResult(
            tool_name="sql_lookup",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message="read-only pool not configured",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    tool_input = ctx.agent_outputs.get("__tool_input__") or {}
    question = (tool_input.get("question") or tool_input.get("query") or ctx.query or "").strip()
    direct_sql = tool_input.get("sql")

    if isinstance(direct_sql, str) and direct_sql.strip():
        emitted_sql = direct_sql.strip()
    else:
        if not question:
            return ToolResult(
                tool_name="sql_lookup",
                success=False,
                data=None,
                error_code="MALFORMED",
                error_message="missing question",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )
        try:
            hint = ctx.agent_outputs.get("__retry_hint__")
            if hint == "schema_simplified":
                schema_text = await _load_compact_schema_text(pool)
                system_prompt = (
                    f"{_SYSTEM_PROMPT}\n\nSchema (table names only):\n{schema_text}"
                )
                user_msg = (
                    f"Question: {question}\n\n"
                    "Return the simplest correct SELECT for this question. "
                    "Call emit_sql."
                )
            else:
                schema_text = await _load_schema_text(pool)
                system_prompt = f"{_SYSTEM_PROMPT}\n\nSchema:\n{schema_text}"
                user_msg = f"Question: {question}\n\nCall emit_sql."
            tool_call = await llm.call_tool(
                system=system_prompt,
                user=user_msg,
                tool=EMIT_SQL_TOOL,
                max_tokens=400,
            )
            emitted_sql = (tool_call.get("sql") or "").strip()
        except Exception as exc:
            return ToolResult(
                tool_name="sql_lookup",
                success=False,
                data=None,
                error_code="MALFORMED",
                error_message=f"LLM error: {exc.__class__.__name__}",
                latency_ms=(time.perf_counter() - started) * 1000.0,
            )

    if not emitted_sql or not _is_select_only(emitted_sql):
        return ToolResult(
            tool_name="sql_lookup",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message=emitted_sql or "empty SQL",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    try:
        columns, rows = await _execute(pool, emitted_sql)
    except asyncpg.exceptions.QueryCanceledError:
        return ToolResult(
            tool_name="sql_lookup",
            success=False,
            data=None,
            error_code="TIMEOUT",
            error_message=f"statement_timeout exceeded ({emitted_sql})",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
    except asyncpg.exceptions.InsufficientPrivilegeError as exc:
        return ToolResult(
            tool_name="sql_lookup",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message=f"insufficient privilege: {exc} ({emitted_sql})",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="sql_lookup",
            success=False,
            data=None,
            error_code="EXEC_ERROR",
            error_message=f"{exc.__class__.__name__}: {exc} ({emitted_sql})",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    latency_ms = (time.perf_counter() - started) * 1000.0
    if not rows:
        return ToolResult(
            tool_name="sql_lookup",
            success=False,
            data={"columns": columns, "rows": [], "sql": emitted_sql},
            error_code="EMPTY",
            error_message="0 rows",
            latency_ms=latency_ms,
        )

    return ToolResult(
        tool_name="sql_lookup",
        success=True,
        data={"columns": columns, "rows": rows, "sql": emitted_sql},
        error_code=None,
        latency_ms=latency_ms,
    )
