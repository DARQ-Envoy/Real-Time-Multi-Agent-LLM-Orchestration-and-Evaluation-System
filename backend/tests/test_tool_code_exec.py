from __future__ import annotations

import pytest

from app.settings import settings
from app.tools import code_exec


async def test_happy_path(shared_ctx, fake_llm):
    shared_ctx.agent_outputs["__tool_input__"] = {"code": "print(2 + 2)"}
    result = await code_exec.run(shared_ctx, fake_llm)
    assert result.success is True
    assert result.data["stdout"].strip() == "4"
    assert result.data["exit_code"] == 0


async def test_timeout(monkeypatch, shared_ctx, fake_llm):
    monkeypatch.setattr(settings, "CODE_EXEC_TIMEOUT_SECONDS", 1)
    shared_ctx.agent_outputs["__tool_input__"] = {"code": "while True:\n    pass"}
    result = await code_exec.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "TIMEOUT"


@pytest.mark.parametrize(
    "code",
    [
        "import os",
        "import subprocess",
        "import socket\nprint('x')",
        "from urllib import request",
        "import requests as r",
        "import httpx",
        "from importlib import import_module",
    ],
)
async def test_malformed_banned_import(shared_ctx, fake_llm, code):
    shared_ctx.agent_outputs["__tool_input__"] = {"code": code}
    result = await code_exec.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "MALFORMED"
    assert "banned import" in (result.error_message or "")


async def test_malformed_syntax_error(shared_ctx, fake_llm):
    shared_ctx.agent_outputs["__tool_input__"] = {"code": "def )("}
    result = await code_exec.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "MALFORMED"


async def test_empty(shared_ctx, fake_llm):
    shared_ctx.agent_outputs["__tool_input__"] = {"code": "pass"}
    result = await code_exec.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "EMPTY"


async def test_exec_error(shared_ctx, fake_llm):
    shared_ctx.agent_outputs["__tool_input__"] = {
        "code": "raise ValueError('boom')"
    }
    result = await code_exec.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "EXEC_ERROR"
    assert "ValueError" in (result.error_message or "") or "boom" in (
        result.error_message or ""
    )


async def test_missing_code(shared_ctx, fake_llm):
    result = await code_exec.run(shared_ctx, fake_llm)
    assert result.success is False
    assert result.error_code == "MALFORMED"
