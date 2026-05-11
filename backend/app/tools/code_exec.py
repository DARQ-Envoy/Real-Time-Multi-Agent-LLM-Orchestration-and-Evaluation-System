"""code_exec tool: AST-checked subprocess sandbox."""

from __future__ import annotations

import ast
import asyncio
import subprocess
import sys
import time

from ..llm import LLMClient
from ..models import SharedContext, ToolResult
from ..settings import settings

BANNED_MODULES = frozenset(
    {
        "os",
        "sys",
        "subprocess",
        "socket",
        "urllib",
        "urllib3",
        "requests",
        "httpx",
        "ctypes",
        "pathlib",
        "shutil",
        "builtins",
        "importlib",
    }
)


def _scan_banned_imports(code: str) -> str | None:
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return f"syntax error: {exc.msg}"
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".", 1)[0]
                if top in BANNED_MODULES:
                    return f"banned import: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            if node.module is None:
                continue
            top = node.module.split(".", 1)[0]
            if top in BANNED_MODULES:
                return f"banned import: {node.module}"
    return None


def _run_subprocess(code: str, timeout_seconds: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-I", "-S", "-c", code],
        timeout=timeout_seconds,
        env={},
        cwd="/tmp",
        capture_output=True,
        text=True,
    )


async def run(ctx: SharedContext, llm: LLMClient) -> ToolResult:
    started = time.perf_counter()
    tool_input = ctx.agent_outputs.get("__tool_input__") or {}
    code = tool_input.get("code")
    if not isinstance(code, str) or not code.strip():
        return ToolResult(
            tool_name="code_exec",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message="missing or empty 'code' input",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    banned = _scan_banned_imports(code)
    if banned is not None:
        return ToolResult(
            tool_name="code_exec",
            success=False,
            data=None,
            error_code="MALFORMED",
            error_message=banned,
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    timeout_seconds = float(settings.CODE_EXEC_TIMEOUT_SECONDS)
    try:
        proc = await asyncio.to_thread(_run_subprocess, code, timeout_seconds)
    except subprocess.TimeoutExpired:
        return ToolResult(
            tool_name="code_exec",
            success=False,
            data=None,
            error_code="TIMEOUT",
            error_message=f"exceeded {timeout_seconds}s",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="code_exec",
            success=False,
            data=None,
            error_code="EXEC_ERROR",
            error_message=f"{exc.__class__.__name__}: {exc}",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    latency_ms = (time.perf_counter() - started) * 1000.0
    stdout = proc.stdout or ""
    stderr = proc.stderr or ""

    if proc.returncode != 0:
        return ToolResult(
            tool_name="code_exec",
            success=False,
            data={"stdout": stdout, "stderr": stderr, "exit_code": proc.returncode},
            error_code="EXEC_ERROR",
            error_message=stderr.strip() or f"exit {proc.returncode}",
            latency_ms=latency_ms,
        )

    if not stdout and not stderr:
        return ToolResult(
            tool_name="code_exec",
            success=False,
            data={"stdout": "", "stderr": "", "exit_code": 0},
            error_code="EMPTY",
            error_message="no output",
            latency_ms=latency_ms,
        )

    return ToolResult(
        tool_name="code_exec",
        success=True,
        data={"stdout": stdout, "stderr": stderr, "exit_code": proc.returncode},
        error_code=None,
        latency_ms=latency_ms,
    )
