from __future__ import annotations

import re
from typing import AsyncIterator

from ..llm import LLMClient
from ..models import (
    AgentEndEvent,
    AgentStartEvent,
    SentenceProvenance,
    SharedContext,
    SSEEvent,
    TokenEvent,
)
from ..persistence import sha256_json
from ._prompt_loader import load_prompt
from .base import AgentBase

LINE_RE = re.compile(r"^\s*\[([^\]]*)\]\s*(.*\S)\s*$")


def parse_synthesis_output(text: str) -> list[SentenceProvenance]:
    out: list[SentenceProvenance] = []
    for line in text.splitlines():
        m = LINE_RE.match(line)
        if not m:
            stripped = line.strip()
            if stripped:
                out.append(
                    SentenceProvenance(
                        sentence_text=stripped,
                        source_agent="synthesis",
                        source_chunk_ids=[],
                    )
                )
            continue
        ids_raw = m.group(1).strip()
        sentence = m.group(2).strip()
        ids = [i.strip() for i in ids_raw.split(",") if i.strip()] if ids_raw else []
        out.append(
            SentenceProvenance(
                sentence_text=sentence,
                source_agent="synthesis",
                source_chunk_ids=ids,
            )
        )
    return out


class SynthesisAgent(AgentBase):
    agent_id = "synthesis"
    max_context_tokens = 8192

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.system_prompt = load_prompt("synthesis")

    async def run(self, ctx: SharedContext) -> AsyncIterator[SSEEvent]:
        yield AgentStartEvent(agent_id=self.agent_id, budget_remaining=self.max_context_tokens)
        chunks_block = "\n".join(
            f"[{c.chunk_id}] {c.text}" for c in ctx.rag_chunks
        ) or "(no evidence chunks retrieved)"
        decomp_block = ""
        if ctx.decomposition and ctx.decomposition.sub_tasks:
            decomp_block = "\nDecomposed sub-tasks:\n" + "\n".join(
                f"- {st.description} ({st.task_type})"
                for st in ctx.decomposition.sub_tasks
            )
        rag_draft = f"\nRAG draft answer:\n{ctx.rag_answer}" if ctx.rag_answer else ""
        critique_block = ""
        if ctx.resolution_loop_active:
            prior = next(
                (
                    cr
                    for cr in reversed(ctx.critique_reports)
                    if cr.target_agent_id == "synthesis"
                ),
                None,
            )
            if prior and prior.reviews:
                lines = [
                    f"- \"{r.span_text}\" (verdict={r.verdict}, "
                    f"confidence={r.confidence_score:.2f}): {r.reason}"
                    for r in prior.reviews
                ]
                critique_block = (
                    "\n\nPrior critique flagged these spans:\n"
                    + "\n".join(lines)
                    + "\nRevise the answer to address each."
                )
        user_msg = (
            f"User query: {ctx.query}\n\n"
            f"Evidence chunks:\n{chunks_block}"
            f"{decomp_block}{rag_draft}{critique_block}\n\n"
            "Produce the final answer now in the required line format."
        )
        accumulated = ""
        violations: str | None = None
        try:
            async for delta in self.llm.stream_text(
                system=self.system_prompt,
                user=user_msg,
                max_tokens=800,
            ):
                accumulated += delta
                yield TokenEvent(agent_id=self.agent_id, text=delta)
        except Exception as exc:
            violations = f"SYNTHESIS_LLM_FAIL:{exc.__class__.__name__}"

        provenance = parse_synthesis_output(accumulated)
        if not provenance and ctx.rag_answer:
            provenance = parse_synthesis_output(ctx.rag_answer)
            violations = (violations or "") + ";FALLBACK_TO_RAG_DRAFT" if violations else "FALLBACK_TO_RAG_DRAFT"
        if not provenance:
            provenance = [
                SentenceProvenance(
                    sentence_text="No supported answer could be produced for this query.",
                    source_agent="synthesis",
                    source_chunk_ids=[],
                )
            ]
            violations = (violations or "") + ";EMPTY_OUTPUT" if violations else "EMPTY_OUTPUT"

        if ctx.agent_outputs.get("web_unavailable"):
            already_marked = (
                provenance
                and provenance[0].source_agent == "web_fallback"
                and provenance[0].sentence_text == "[WEB_UNAVAILABLE]"
            )
            if not already_marked:
                provenance = [
                    SentenceProvenance(
                        sentence_text="[WEB_UNAVAILABLE]",
                        source_agent="web_fallback",
                        source_chunk_ids=[],
                    ),
                    *provenance,
                ]

        ctx.final_answer = provenance
        output_hash = sha256_json([p.model_dump() for p in provenance])
        yield AgentEndEvent(
            agent_id=self.agent_id,
            output_hash=output_hash or "",
            policy_violations=violations,
        )
