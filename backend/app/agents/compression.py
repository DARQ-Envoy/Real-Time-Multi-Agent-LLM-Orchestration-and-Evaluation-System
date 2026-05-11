"""CompressionAgent — lossless sidecar persistence + lossy rag_answer summary.

Triggered by the pipeline when an agent yields BudgetRequestEvent. Idempotent:
if ctx.compressed is already set, returns immediately with no DB writes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import uuid
from typing import Any, AsyncIterator

from ..budget import count_tokens
from ..llm import LLMClient
from ..models import (
    AgentEndEvent,
    AgentStartEvent,
    CompressedContext,
    SharedContext,
    SSEEvent,
)
from ..persistence import sha256_json
from ..settings import settings
from .base import AgentBase

_log = logging.getLogger(__name__)

_SUMMARY_SYSTEM = (
    "Compress the following draft answer into ONE paragraph (<=300 characters) "
    "that preserves all factual claims and citation chunk_ids. Drop hedging "
    "language. Output only the paragraph."
)


def _hash_payload(payload: Any) -> str:
    blob = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()


class CompressionAgent(AgentBase):
    agent_id = "compression"

    def __init__(self, llm: LLMClient, db_pool) -> None:
        self.llm = llm
        self.db_pool = db_pool
        self.max_context_tokens = settings.BUDGET_COMPRESSION

    async def run(self, ctx: SharedContext) -> AsyncIterator[SSEEvent]:
        yield AgentStartEvent(
            agent_id=self.agent_id, budget_remaining=self.max_context_tokens
        )

        if ctx.compressed is not None:
            yield AgentEndEvent(
                agent_id=self.agent_id,
                output_hash=sha256_json(ctx.compressed.model_dump()) or "",
                policy_violations="COMPRESSION_SKIPPED_IDEMPOTENT",
            )
            return

        original_tokens = self._estimate_total_tokens(ctx)
        lossless_fields: list[str] = []
        job_uuid = uuid.UUID(ctx.job_id)

        # --- Lossless: tool outputs ---
        tools_bucket = ctx.agent_outputs.get("tools") or {}
        for tool_name, entries in tools_bucket.items():
            if not isinstance(entries, list):
                continue
            for idx, entry in enumerate(entries):
                payload = entry.get("data") if isinstance(entry, dict) else entry
                if payload is None:
                    continue
                ok = await self._persist_sidecar(
                    job_uuid, payload, field_kind="tool_output"
                )
                if ok:
                    lossless_fields.append(f"tools.{tool_name}[{idx}]")

        # --- Lossless: RAG chunks (citations) ---
        for idx, chunk in enumerate(ctx.rag_chunks):
            ok = await self._persist_sidecar(
                job_uuid, chunk.model_dump(), field_kind="citations"
            )
            if ok:
                lossless_fields.append(f"rag_chunks[{idx}]")

        # --- Lossy: summarize rag_answer if present ---
        summary = ""
        if ctx.rag_answer and ctx.rag_answer.strip():
            summary = await self._summarize(ctx.rag_answer)

        compressed_tokens = (
            count_tokens(summary)
            + sum(count_tokens(c.text[:32]) for c in ctx.rag_chunks)  # references only
        )
        ratio = (
            (compressed_tokens / original_tokens)
            if original_tokens > 0
            else 1.0
        )
        if ratio > 1.0 or original_tokens == 0:
            ratio = 1.0

        ctx.compressed = CompressedContext(
            compression_ratio=ratio,
            lossless_fields_preserved=lossless_fields,
            summary=summary,
        )

        yield AgentEndEvent(
            agent_id=self.agent_id,
            output_hash=sha256_json(ctx.compressed.model_dump()) or "",
            policy_violations=None,
        )

    def _estimate_total_tokens(self, ctx: SharedContext) -> int:
        total = 0
        for c in ctx.rag_chunks:
            total += count_tokens(c.text or "")
        if ctx.rag_answer:
            total += count_tokens(ctx.rag_answer)
        tools = ctx.agent_outputs.get("tools") or {}
        for entries in tools.values():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                if isinstance(entry, dict):
                    total += count_tokens(json.dumps(entry.get("data") or ""))
                else:
                    total += count_tokens(str(entry))
        return total

    async def _persist_sidecar(
        self, job_uuid: uuid.UUID, payload: Any, field_kind: str
    ) -> bool:
        try:
            field_hash = _hash_payload(payload)
            async with self.db_pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO compressed_sidecars
                        (job_id, field_hash, field_kind, content)
                    VALUES ($1, $2, $3, $4::jsonb)
                    """,
                    job_uuid,
                    field_hash,
                    field_kind,
                    json.dumps(payload, default=str),
                )
            return True
        except Exception as exc:
            _log.warning("compressed_sidecars INSERT failed: %s", exc)
            return False

    async def _summarize(self, text: str) -> str:
        try:
            accumulated = ""
            async for delta in self.llm.stream_text(
                system=_SUMMARY_SYSTEM,
                user=f"Draft answer:\n{text}",
                max_tokens=256,
            ):
                accumulated += delta
            return accumulated.strip()[:300]
        except Exception as exc:
            _log.warning("compression summary failed: %s", exc)
            return ""


__all__ = ["CompressionAgent"]
