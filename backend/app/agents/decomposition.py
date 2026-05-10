from __future__ import annotations

import json
import re
from typing import AsyncIterator

from ..llm import LLMClient
from ..models import (
    AgentEndEvent,
    AgentStartEvent,
    DecompositionResult,
    SharedContext,
    SSEEvent,
    SubTask,
    TokenEvent,
)
from ..persistence import sha256_json
from ._prompt_loader import load_prompt
from .base import AgentBase

JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


def _default_subtasks(query: str) -> list[SubTask]:
    return [
        SubTask(
            task_id="t1",
            task_type="FACTUAL",
            description=f"Identify the factual basis for: {query}",
            depends_on=[],
            priority=1,
        ),
        SubTask(
            task_id="t2",
            task_type="ANALYTICAL",
            description=f"Synthesize a coherent answer to: {query}",
            depends_on=["t1"],
            priority=2,
        ),
    ]


class DecompositionAgent(AgentBase):
    agent_id = "decomposition"
    max_context_tokens = 3072

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.system_prompt = load_prompt("decomp")

    async def run(self, ctx: SharedContext) -> AsyncIterator[SSEEvent]:
        yield AgentStartEvent(agent_id=self.agent_id, budget_remaining=self.max_context_tokens)
        accumulated = ""
        violations: str | None = None
        try:
            async for delta in self.llm.stream_text(
                system=self.system_prompt,
                user=f"Query:\n{ctx.query}",
                max_tokens=512,
            ):
                accumulated += delta
                yield TokenEvent(agent_id=self.agent_id, text=delta)
            sub_tasks = _parse_subtasks(accumulated)
        except Exception:
            sub_tasks = _default_subtasks(ctx.query)
            violations = "DECOMP_LLM_FAILURE"
        if not sub_tasks or len(sub_tasks) < 2:
            sub_tasks = _default_subtasks(ctx.query)
            violations = violations or "DECOMP_BELOW_MIN_SUBTASKS"
        result = DecompositionResult(sub_tasks=sub_tasks)
        ctx.decomposition = result
        output_hash = sha256_json(result.model_dump())
        yield AgentEndEvent(
            agent_id=self.agent_id,
            output_hash=output_hash or "",
            policy_violations=violations,
        )


def _parse_subtasks(text: str) -> list[SubTask]:
    match = JSON_ARRAY_RE.search(text)
    if not match:
        return []
    try:
        raw = json.loads(match.group(0))
    except json.JSONDecodeError:
        return []
    out: list[SubTask] = []
    for item in raw:
        try:
            out.append(SubTask(**item))
        except Exception:
            continue
    return out
