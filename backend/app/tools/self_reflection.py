"""self_reflection tool: identify contradictions between earlier turns."""

from __future__ import annotations

import time
from typing import Any

from ..llm import LLMClient
from ..models import SharedContext, ToolResult

CONTRADICTIONS_TOOL: dict[str, Any] = {
    "name": "emit_contradictions",
    "description": (
        "Emit logical contradictions between two or more earlier turns. "
        "Return an empty array if none exist."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "contradictions": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "turn_a": {"type": "string"},
                        "turn_b": {"type": "string"},
                        "description": {"type": "string"},
                    },
                    "required": ["turn_a", "turn_b", "description"],
                },
            }
        },
        "required": ["contradictions"],
    },
}

_SYSTEM_PROMPT = (
    "You compare an agent's earlier outputs in a session and identify logical "
    "contradictions between them. Always call the emit_contradictions tool, "
    "even if the contradictions array is empty."
)


def _collect_turns(ctx: SharedContext) -> list[tuple[str, str]]:
    turns: list[tuple[str, str]] = []
    if ctx.rag_answer:
        turns.append(("rag", ctx.rag_answer.strip()))
    if ctx.final_answer:
        synth_text = "\n".join(p.sentence_text for p in ctx.final_answer).strip()
        if synth_text:
            turns.append(("synthesis", synth_text))
    return turns


async def run(ctx: SharedContext, llm: LLMClient) -> ToolResult:
    started = time.perf_counter()
    turns = _collect_turns(ctx)
    if len(turns) < 1:
        return ToolResult(
            tool_name="self_reflection",
            success=False,
            data=None,
            error_code="EMPTY",
            error_message="No prior agent outputs to reflect on.",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )

    body = "\n\n".join(f"=== {tid} ===\n{txt}" for tid, txt in turns)
    user_msg = (
        f"Earlier turns:\n\n{body}\n\n"
        "Call emit_contradictions now. Return [] if none exist."
    )

    try:
        tool_input = await llm.call_tool(
            system=_SYSTEM_PROMPT,
            user=user_msg,
            tool=CONTRADICTIONS_TOOL,
            max_tokens=600,
        )
        contradictions = tool_input.get("contradictions") or []
        latency_ms = (time.perf_counter() - started) * 1000.0
        if not isinstance(contradictions, list):
            return ToolResult(
                tool_name="self_reflection",
                success=False,
                data=None,
                error_code="MALFORMED",
                error_message="Tool returned non-list contradictions.",
                latency_ms=latency_ms,
            )
        return ToolResult(
            tool_name="self_reflection",
            success=True,
            data=contradictions,
            error_code=None,
            latency_ms=latency_ms,
        )
    except Exception as exc:
        return ToolResult(
            tool_name="self_reflection",
            success=False,
            data=None,
            error_code="EXEC_ERROR",
            error_message=f"{exc.__class__.__name__}: {exc}",
            latency_ms=(time.perf_counter() - started) * 1000.0,
        )
