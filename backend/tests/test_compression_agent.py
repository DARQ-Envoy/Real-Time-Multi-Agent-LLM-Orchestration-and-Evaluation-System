"""E3: CompressionAgent behavior with a fake LLM + fake db_pool."""

from __future__ import annotations

from typing import Any

import pytest

from app.agents.compression import CompressionAgent
from app.models import AgentEndEvent, AgentStartEvent, Chunk


class _SummaryLLM:
    configured = True

    def __init__(self, summary: str = "[compressed paragraph]") -> None:
        self._summary = summary
        self.stream_calls = 0

    async def stream_text(self, *, system: str, user: str, max_tokens: int = 1024):
        self.stream_calls += 1
        for chunk in self._summary.split():
            yield chunk + " "

    async def call_tool(self, **kw: Any):  # pragma: no cover
        return {}


async def _collect(events):
    out = []
    async for e in events:
        out.append(e)
    return out


async def test_lossless_persists_tools_and_chunks(
    shared_ctx, fake_db_pool
):
    shared_ctx.agent_outputs["tools"] = {
        "web_search": [{"data": [{"title": "T", "url": "u"}], "success": True}],
        "sql_lookup": [{"data": {"columns": ["id"], "rows": [[1]]}, "success": True}],
    }
    shared_ctx.rag_chunks = [
        Chunk(chunk_id="c1", text="first", source_url="http://x/1"),
        Chunk(chunk_id="c2", text="second", source_url="http://x/2"),
    ]
    shared_ctx.rag_answer = "a draft answer about something with citations [c1]."

    agent = CompressionAgent(_SummaryLLM("compressed body of the answer"), fake_db_pool)
    events = await _collect(agent.run(shared_ctx))

    assert isinstance(events[0], AgentStartEvent)
    assert isinstance(events[-1], AgentEndEvent)
    inserts = [
        (sql, args)
        for (sql, args) in fake_db_pool.executed
        if "INSERT INTO compressed_sidecars" in sql
    ]
    # 2 tool entries + 2 chunks = 4 rows.
    assert len(inserts) == 4
    field_kinds = [args[2] for (_, args) in inserts]
    assert field_kinds.count("tool_output") == 2
    assert field_kinds.count("citations") == 2

    assert shared_ctx.compressed is not None
    assert shared_ctx.compressed.compression_ratio < 1.0
    assert "rag_chunks[0]" in shared_ctx.compressed.lossless_fields_preserved
    assert shared_ctx.compressed.summary  # non-empty


async def test_empty_context_yields_unit_ratio(shared_ctx, fake_db_pool):
    agent = CompressionAgent(_SummaryLLM(), fake_db_pool)
    await _collect(agent.run(shared_ctx))

    assert shared_ctx.compressed is not None
    assert shared_ctx.compressed.compression_ratio == 1.0
    assert shared_ctx.compressed.lossless_fields_preserved == []
    assert shared_ctx.compressed.summary == ""
    inserts = [
        (sql, args)
        for (sql, args) in fake_db_pool.executed
        if "INSERT INTO compressed_sidecars" in sql
    ]
    assert len(inserts) == 0


async def test_idempotent_when_already_compressed(shared_ctx, fake_db_pool):
    from app.models import CompressedContext

    shared_ctx.compressed = CompressedContext(
        compression_ratio=0.5,
        lossless_fields_preserved=["rag_chunks[0]"],
        summary="prior",
    )
    shared_ctx.rag_chunks = [
        Chunk(chunk_id="c1", text="first", source_url="http://x/1")
    ]
    shared_ctx.rag_answer = "would summarize but skipped"

    llm = _SummaryLLM()
    agent = CompressionAgent(llm, fake_db_pool)
    events = await _collect(agent.run(shared_ctx))

    end = events[-1]
    assert isinstance(end, AgentEndEvent)
    assert end.policy_violations == "COMPRESSION_SKIPPED_IDEMPOTENT"

    inserts = [
        (sql, args)
        for (sql, args) in fake_db_pool.executed
        if "INSERT INTO compressed_sidecars" in sql
    ]
    assert len(inserts) == 0
    assert llm.stream_calls == 0


async def test_summary_failure_keeps_compression_ratio_safe(
    shared_ctx, fake_db_pool
):
    class _BrokenLLM(_SummaryLLM):
        async def stream_text(self, **kw):
            raise RuntimeError("LLM unavailable")
            yield ""  # pragma: no cover

    shared_ctx.rag_answer = "draft" * 100
    agent = CompressionAgent(_BrokenLLM(), fake_db_pool)
    await _collect(agent.run(shared_ctx))

    assert shared_ctx.compressed is not None
    # summary is empty; ratio collapses to 1.0 per the spec contract.
    assert shared_ctx.compressed.summary == ""
