from __future__ import annotations

from typing import AsyncIterator

from ..corpus import keyword_search
from ..llm import LLMClient
from ..models import (
    AgentEndEvent,
    AgentStartEvent,
    Chunk,
    SharedContext,
    SSEEvent,
    TokenEvent,
)
from ..persistence import sha256_json
from ._prompt_loader import load_prompt
from .base import AgentBase

K_PER_HOP = 3


def _format_chunks(chunks: list[dict]) -> str:
    return "\n".join(f"[{c['chunk_id']}] {c['text']}" for c in chunks)


def _dedupe_in_order(*chunk_lists: list[dict]) -> list[dict]:
    seen: set[str] = set()
    out: list[dict] = []
    for chunks in chunk_lists:
        for c in chunks:
            if c["chunk_id"] in seen:
                continue
            seen.add(c["chunk_id"])
            out.append(c)
    return out


class RAGAgent(AgentBase):
    agent_id = "rag"
    max_context_tokens = 6144

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.system_prompt = load_prompt("rag")

    async def run(self, ctx: SharedContext) -> AsyncIterator[SSEEvent]:
        yield AgentStartEvent(agent_id=self.agent_id, budget_remaining=self.max_context_tokens)

        hop1 = keyword_search(ctx.query, k=K_PER_HOP)
        for c in hop1:
            c["hop_number"] = 1

        violations: str | None = None
        reformulated = ctx.query
        if hop1:
            reformulate_user = (
                f"Mode: Reformulate.\nOriginal query: {ctx.query}\n\n"
                f"Hop 1 chunks:\n{_format_chunks(hop1)}\n\n"
                "Output ONLY the reformulated query."
            )
            try:
                accum = ""
                async for delta in self.llm.stream_text(
                    system=self.system_prompt,
                    user=reformulate_user,
                    max_tokens=128,
                ):
                    accum += delta
                cleaned = accum.strip().strip('"').strip("'").splitlines()[0].strip()
                if cleaned:
                    reformulated = cleaned
                else:
                    violations = "REFORMULATION_EMPTY"
            except Exception:
                violations = "REFORMULATION_PARSE_FAIL"

        hop2 = keyword_search(reformulated, k=K_PER_HOP)
        for c in hop2:
            c["hop_number"] = 2

        merged = _dedupe_in_order(hop1, hop2)
        ctx.rag_chunks = [Chunk(**c) for c in merged]
        ctx.low_coverage = not merged

        if not merged:
            violations = (violations + ";LOW_COVERAGE") if violations else "LOW_COVERAGE"
            ctx.rag_answer = ""
            output_hash = sha256_json({"chunks": [], "answer": ""})
            yield AgentEndEvent(
                agent_id=self.agent_id,
                output_hash=output_hash or "",
                policy_violations=violations,
            )
            return

        draft_user = (
            f"Mode: Draft.\nOriginal query: {ctx.query}\n\n"
            f"Evidence chunks:\n{_format_chunks(merged)}\n\n"
            "Draft the answer now."
        )
        answer = ""
        try:
            async for delta in self.llm.stream_text(
                system=self.system_prompt,
                user=draft_user,
                max_tokens=600,
            ):
                answer += delta
                yield TokenEvent(agent_id=self.agent_id, text=delta)
        except Exception as exc:
            violations = (violations + f";DRAFT_FAIL:{exc.__class__.__name__}") if violations else f"DRAFT_FAIL:{exc.__class__.__name__}"

        ctx.rag_answer = answer
        output_hash = sha256_json(
            {"chunks": [c["chunk_id"] for c in merged], "answer": answer}
        )
        yield AgentEndEvent(
            agent_id=self.agent_id,
            output_hash=output_hash or "",
            policy_violations=violations,
        )
