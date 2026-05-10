from __future__ import annotations

from typing import Any, AsyncIterator

from ..llm import LLMClient
from ..models import (
    AgentEndEvent,
    AgentStartEvent,
    ClaimReview,
    CritiqueReport,
    SharedContext,
    SSEEvent,
)
from ..persistence import sha256_json
from ._prompt_loader import load_prompt
from .base import AgentBase

CRITIQUE_TOOL: dict[str, Any] = {
    "name": "emit_critique_report",
    "description": "Emit claim-level review of the target agent's output.",
    "input_schema": {
        "type": "object",
        "properties": {
            "reviews": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "span_text": {
                            "type": "string",
                            "description": "Verbatim substring of the target output.",
                        },
                        "confidence_score": {
                            "type": "number",
                            "minimum": 0,
                            "maximum": 1,
                        },
                        "verdict": {
                            "type": "string",
                            "enum": ["SUPPORTED", "UNSUPPORTED", "UNCERTAIN"],
                        },
                        "reason": {"type": "string"},
                    },
                    "required": [
                        "span_text",
                        "confidence_score",
                        "verdict",
                        "reason",
                    ],
                },
            }
        },
        "required": ["reviews"],
    },
}


def _target_output_text(ctx: SharedContext, target: str) -> str:
    if target == "decomposition" and ctx.decomposition:
        return "\n".join(
            f"- ({st.task_type}) {st.description}" for st in ctx.decomposition.sub_tasks
        )
    if target == "rag":
        return ctx.rag_answer or ""
    if target == "synthesis":
        return "\n".join(p.sentence_text for p in ctx.final_answer)
    return ""


def _evidence_text(ctx: SharedContext, target: str) -> str:
    if target == "synthesis" or target == "rag":
        if not ctx.rag_chunks:
            return "(no retrieved evidence chunks)"
        return "\n".join(f"[{c.chunk_id}] {c.text}" for c in ctx.rag_chunks)
    if target == "decomposition":
        return f"User query: {ctx.query}"
    return ""


class CritiqueAgent(AgentBase):
    max_context_tokens = 4096

    def __init__(self, llm: LLMClient, target_agent_id: str) -> None:
        self.llm = llm
        self.target_agent_id = target_agent_id
        self.agent_id = f"critique:{target_agent_id}"
        self.system_prompt = load_prompt("critique")

    async def run(self, ctx: SharedContext) -> AsyncIterator[SSEEvent]:
        yield AgentStartEvent(
            agent_id=self.agent_id, budget_remaining=self.max_context_tokens
        )
        target_output = _target_output_text(ctx, self.target_agent_id).strip()
        evidence = _evidence_text(ctx, self.target_agent_id).strip()
        violations: str | None = None
        reviews: list[ClaimReview] = []

        if not target_output:
            violations = "CRITIQUE_TARGET_EMPTY"
        else:
            user_msg = (
                f"Target agent: {self.target_agent_id}\n\n"
                f"Output to review:\n{target_output}\n\n"
                f"Evidence basis:\n{evidence}\n\n"
                "Emit your reviews via the tool now."
            )
            try:
                tool_input = await self.llm.call_tool(
                    system=self.system_prompt,
                    user=user_msg,
                    tool=CRITIQUE_TOOL,
                    max_tokens=800,
                )
                raw_reviews = tool_input.get("reviews") or []
                for raw in raw_reviews:
                    try:
                        reviews.append(ClaimReview(**raw))
                    except Exception:
                        continue
            except Exception as exc:
                violations = f"CRITIQUE_LLM_FAIL:{exc.__class__.__name__}"

        if not reviews and violations is None:
            violations = "CRITIQUE_EMPTY"

        report = CritiqueReport(target_agent_id=self.target_agent_id, reviews=reviews)
        ctx.critique_reports.append(report)
        output_hash = sha256_json(report.model_dump())
        yield AgentEndEvent(
            agent_id=self.agent_id,
            output_hash=output_hash or "",
            policy_violations=violations,
        )
