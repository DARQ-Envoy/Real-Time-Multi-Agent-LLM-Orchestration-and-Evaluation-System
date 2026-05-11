"""Shared fixtures for tool + pipeline tests."""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.models import SharedContext


class FakeLLM:
    """Stand-in for LLMClient. Tests that need it can subclass or set attributes."""

    configured = True

    def __init__(self, tool_response: dict[str, Any] | None = None) -> None:
        self.tool_response = tool_response or {}
        self.calls: list[dict[str, Any]] = []

    async def call_tool(self, *, system: str, user: str, tool: dict[str, Any], max_tokens: int = 1024) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "tool": tool["name"]})
        return self.tool_response

    async def stream_text(self, *, system: str, user: str, max_tokens: int = 1024):
        if False:
            yield ""  # pragma: no cover


class ScriptedLLM(FakeLLM):
    """Returns a different tool_response on each successive call_tool invocation."""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        super().__init__()
        self.responses = list(responses)
        self.cursor = 0

    async def call_tool(self, *, system: str, user: str, tool: dict[str, Any], max_tokens: int = 1024) -> dict[str, Any]:
        self.calls.append({"system": system, "user": user, "tool": tool["name"]})
        if self.cursor >= len(self.responses):
            return {}
        out = self.responses[self.cursor]
        self.cursor += 1
        return out


class FakeConnection:
    def __init__(self, sink: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._sink = sink

    async def execute(self, sql: str, *args: Any) -> None:
        self._sink.append((sql, args))

    async def fetch(self, sql: str, *args: Any):
        return []

    async def fetchrow(self, sql: str, *args: Any):
        return None

    async def fetchval(self, sql: str, *args: Any):
        return None


class FakeAcquireCtx:
    def __init__(self, conn: FakeConnection) -> None:
        self._conn = conn

    async def __aenter__(self) -> FakeConnection:
        return self._conn

    async def __aexit__(self, *_: Any) -> None:
        return None


class FakeDbPool:
    """Minimal asyncpg.Pool stand-in that records every `execute` call."""

    def __init__(self) -> None:
        self.executed: list[tuple[str, tuple[Any, ...]]] = []
        self._conn = FakeConnection(self.executed)

    def acquire(self) -> FakeAcquireCtx:
        return FakeAcquireCtx(self._conn)


class _FakePipeline:
    def __init__(self, sink: list[tuple[str, tuple[Any, ...]]]) -> None:
        self._sink = sink

    async def __aenter__(self) -> "_FakePipeline":
        return self

    async def __aexit__(self, *_: Any) -> None:
        return None

    def rpush(self, *args: Any) -> None:
        self._sink.append(("rpush", args))

    def expire(self, *args: Any) -> None:
        self._sink.append(("expire", args))

    async def execute(self) -> None:
        return None


class FakeRedis:
    """Captures pipeline commands. Sufficient for redis_bus.publish_event."""

    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[Any, ...]]] = []

    def pipeline(self, transaction: bool = False) -> _FakePipeline:
        return _FakePipeline(self.commands)

    async def lrange(self, *_: Any) -> list[bytes]:
        return []


@pytest.fixture
def shared_ctx() -> SharedContext:
    return SharedContext(job_id=str(uuid.uuid4()), query="what is the retry policy?")


@pytest.fixture
def fake_llm() -> FakeLLM:
    return FakeLLM()


@pytest.fixture
def fake_db_pool() -> FakeDbPool:
    return FakeDbPool()


@pytest.fixture
def fake_redis() -> FakeRedis:
    return FakeRedis()
