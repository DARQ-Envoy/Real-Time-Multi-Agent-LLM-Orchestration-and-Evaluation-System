from __future__ import annotations

from typing import Any, AsyncIterator

from ..llm import LLMClient
from ..models import (
    AgentEndEvent,
    AgentStartEvent,
    PlannedToolCall,
    RoutingPlan,
    SharedContext,
    SSEEvent,
)
from ..persistence import sha256_json
from ._prompt_loader import load_prompt
from .base import AgentBase

ALLOWED_TOOLS = {"web_search", "code_exec", "sql_lookup"}
# self_reflection is run implicitly by the pipeline and is not Orchestrator-routable.
# critique runs after every agent; the dispatch loop iterates AGENT_REGISTRY only,
# so planning tool_calls for critique would be silently dropped — exclude it.
ALLOWED_TOOL_AGENTS = {"decomposition", "rag", "synthesis"}

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
            "tool_calls": {
                "type": "array",
                "description": (
                    "Optional. Tool invocations to run BEFORE the named agent runs. "
                    "Omit unless a tool is genuinely needed."
                ),
                "items": {
                    "type": "object",
                    "properties": {
                        "agent_id": {
                            "type": "string",
                            "description": "Which agent this tool feeds (rag, synthesis, etc.)",
                        },
                        "tool_name": {
                            "type": "string",
                            "enum": sorted(ALLOWED_TOOLS),
                        },
                        "input": {
                            "type": "object",
                            "description": "Tool-specific arguments.",
                            "additionalProperties": True,
                        },
                    },
                    "required": ["agent_id", "tool_name", "input"],
                },
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


def _parse_tool_calls(raw: Any) -> list[PlannedToolCall]:
    if not isinstance(raw, list):
        return []
    out: list[PlannedToolCall] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        agent_id = item.get("agent_id")
        tool_name = item.get("tool_name")
        tool_input = item.get("input")
        if not isinstance(agent_id, str) or agent_id not in ALLOWED_TOOL_AGENTS:
            continue
        if not isinstance(tool_name, str) or tool_name not in ALLOWED_TOOLS:
            continue
        if not isinstance(tool_input, dict):
            tool_input = {}
        out.append(
            PlannedToolCall(agent_id=agent_id, tool_name=tool_name, input=tool_input)
        )
    return out


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
                tool_calls=_parse_tool_calls(tool_input.get("tool_calls")),
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
