from __future__ import annotations

from typing import Any, AsyncIterator

from ..llm import LLMClient
from ..models import (
    AgentEndEvent,
    AgentStartEvent,
    RoutingPlan,
    SharedContext,
    SSEEvent,
)
from ..persistence import sha256_json
from ._prompt_loader import load_prompt
from .base import AgentBase

ROUTING_PLAN_TOOL: dict[str, Any] = {
    "name": "emit_routing_plan",
    "description": (
        "Emit the ordered agent execution plan for this query. "
        "Use only the agents listed in the system prompt."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "agent_sequence": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Ordered list of agent IDs to run.",
            },
            "justification": {
                "type": "string",
                "description": "One-sentence reason for this plan.",
            },
        },
        "required": ["agent_sequence", "justification"],
    },
}

DEFAULT_FALLBACK_PLAN = RoutingPlan(
    agent_sequence=["rag", "synthesis"],
    justification="default fallback (orchestrator output invalid or missing fields)",
)


class OrchestratorAgent(AgentBase):
    agent_id = "orchestrator"
    max_context_tokens = 4096

    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm
        self.system_prompt = load_prompt("orchestrator")

    async def run(self, ctx: SharedContext) -> AsyncIterator[SSEEvent]:
        yield AgentStartEvent(agent_id=self.agent_id, budget_remaining=self.max_context_tokens)
        user_msg = f"User query:\n{ctx.query}"
        try:
            tool_input = await self.llm.call_tool(
                system=self.system_prompt,
                user=user_msg,
                tool=ROUTING_PLAN_TOOL,
                max_tokens=512,
            )
            plan = RoutingPlan(
                agent_sequence=list(tool_input.get("agent_sequence") or []),
                justification=str(tool_input.get("justification") or ""),
            )
            if not plan.agent_sequence or "synthesis" not in plan.agent_sequence:
                plan = DEFAULT_FALLBACK_PLAN.model_copy()
        except Exception:
            plan = DEFAULT_FALLBACK_PLAN.model_copy()
        ctx.routing_plan = plan
        output_hash = sha256_json(plan.model_dump())
        yield AgentEndEvent(
            agent_id=self.agent_id,
            output_hash=output_hash or "",
            policy_violations=None,
        )
