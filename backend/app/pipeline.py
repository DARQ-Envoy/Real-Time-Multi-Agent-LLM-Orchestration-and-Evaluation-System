"""Routing/dispatch pipeline. Owns SSE publication and agent_logs persistence."""

from __future__ import annotations

import time
from typing import Any

from .agents.base import AgentBase
from .agents.compression import CompressionAgent
from .agents.critique import CritiqueAgent
from .agents.decomposition import DecompositionAgent
from .agents.orchestrator import OrchestratorAgent
from .agents.rag import RAGAgent
from .agents.synthesis import SynthesisAgent
from .budget import ContextBudgetManager, count_tokens
from .llm import LLMClient, LLMNotConfigured
from .models import (
    AgentEndEvent,
    BudgetRequestEvent,
    ErrorEvent,
    SentenceProvenance,
    SharedContext,
    TokenEvent,
)
from .persistence import log_agent_event, merge_violations, sha256_json
from .redis_bus import publish_event
from .settings import settings
from .tools import self_reflection
from .tools.registry import REGISTRY as TOOL_REGISTRY
from .tools.runner import run_with_retry

AGENT_REGISTRY: dict[str, type[AgentBase]] = {
    "decomposition": DecompositionAgent,
    "rag": RAGAgent,
    "synthesis": SynthesisAgent,
}

LOW_CONFIDENCE_THRESHOLD = 0.4


async def run(shared_ctx: SharedContext, redis, db_pool) -> list[SentenceProvenance]:
    """Execute the agent pipeline. Publishes SSE events and writes agent_logs.

    Returns the final SentenceProvenance list (empty on failure).
    """
    job_id = shared_ctx.job_id

    try:
        llm = LLMClient()
        if not llm.configured:
            raise LLMNotConfigured(
                "LLM_API_KEY missing or set to stub default."
            )
    except LLMNotConfigured as exc:
        await publish_event(
            redis,
            job_id,
            ErrorEvent(
                error_code="LLM_NOT_CONFIGURED",
                message=str(exc),
                job_id=job_id,
            ).model_dump(),
        )
        return []

    budget_manager = ContextBudgetManager()
    shared_ctx.agent_outputs["__budget_manager__"] = budget_manager

    orchestrator = OrchestratorAgent(llm)
    await _run_agent(orchestrator, shared_ctx, redis, db_pool, budget_manager)

    sequence = (
        list(shared_ctx.routing_plan.agent_sequence)
        if shared_ctx.routing_plan
        else ["rag", "synthesis"]
    )
    if settings.CUT_DECOMPOSITION:
        sequence = [a for a in sequence if a != "decomposition"]
    if "rag" not in sequence:
        sequence.insert(0, "rag")
    if "synthesis" not in sequence:
        sequence.append("synthesis")
    if sequence.index("synthesis") != len(sequence) - 1:
        sequence = [a for a in sequence if a != "synthesis"] + ["synthesis"]

    for agent_id in sequence:
        if agent_id == "orchestrator":
            continue
        cls = AGENT_REGISTRY.get(agent_id)
        if cls is None:
            continue
        await _dispatch_tool_calls(agent_id, shared_ctx, llm, db_pool, redis)
        agent = cls(llm)
        budget_requested = await _run_agent(
            agent, shared_ctx, redis, db_pool, budget_manager
        )

        # E3 compression-and-rerun loop: if the agent yielded a
        # BudgetRequestEvent and compression hasn't already run for this job,
        # invoke CompressionAgent once and re-run the originating agent ONCE.
        if budget_requested and shared_ctx.compressed is None:
            compression = CompressionAgent(llm, db_pool)
            await _run_agent(compression, shared_ctx, redis, db_pool, budget_manager)
            agent = cls(llm)
            await _run_agent(agent, shared_ctx, redis, db_pool, budget_manager)

        critic = CritiqueAgent(llm, target_agent_id=agent_id)
        await _run_agent(critic, shared_ctx, redis, db_pool, budget_manager)

    await run_with_retry(
        self_reflection.run,
        shared_ctx,
        llm,
        db_pool,
        redis,
        tool_name="self_reflection",
        max_retries=0,
    )

    if not settings.CUT_RESOLUTION_LOOP and _synthesis_needs_resolution(shared_ctx):
        shared_ctx.resolution_loop_active = True
        synth = SynthesisAgent(llm)
        await _run_agent(
            synth,
            shared_ctx,
            redis,
            db_pool,
            budget_manager,
            extra_violation="RESOLUTION_LOOP_RUN",
        )

    _dump_critique_summary(shared_ctx)
    return shared_ctx.final_answer


async def _dispatch_tool_calls(
    agent_id: str,
    ctx: SharedContext,
    llm: LLMClient,
    db_pool,
    redis,
) -> None:
    """Run any RoutingPlan.tool_calls keyed to this agent through run_with_retry."""
    plan = ctx.routing_plan
    if plan is None or not plan.tool_calls:
        return
    pending = [tc for tc in plan.tool_calls if tc.agent_id == agent_id]
    if not pending:
        return
    tools_bucket = ctx.agent_outputs.setdefault("tools", {})
    for planned in pending:
        tool_fn = TOOL_REGISTRY.get(planned.tool_name)
        if tool_fn is None:
            continue
        ctx.agent_outputs["__tool_input__"] = dict(planned.input)
        try:
            result = await run_with_retry(
                tool_fn,
                ctx,
                llm,
                db_pool,
                redis,
                tool_name=planned.tool_name,
                input_payload={
                    "agent_id": planned.agent_id,
                    "tool_name": planned.tool_name,
                    "input": dict(planned.input),
                },
            )
        finally:
            ctx.agent_outputs.pop("__tool_input__", None)
        tools_bucket.setdefault(planned.tool_name, []).append(
            {
                "input": planned.input,
                "success": result.success,
                "data": result.data,
                "error_code": result.error_code,
                "latency_ms": result.latency_ms,
                "accepted_by_agent": result.accepted_by_agent,
            }
        )


def _dump_critique_summary(ctx: SharedContext) -> None:
    """Print critique verdict counts to worker stdout for AC inspection."""
    if not ctx.critique_reports:
        return
    print(f"[critique-summary job={ctx.job_id}]", flush=True)
    for report in ctx.critique_reports:
        counts = {"SUPPORTED": 0, "UNSUPPORTED": 0, "UNCERTAIN": 0}
        for r in report.reviews:
            counts[r.verdict] = counts.get(r.verdict, 0) + 1
        print(
            f"  {report.target_agent_id}: "
            f"S={counts['SUPPORTED']} U={counts['UNSUPPORTED']} "
            f"Q={counts['UNCERTAIN']} (n={len(report.reviews)})",
            flush=True,
        )


def _synthesis_needs_resolution(ctx: SharedContext) -> bool:
    report = next(
        (cr for cr in reversed(ctx.critique_reports) if cr.target_agent_id == "synthesis"),
        None,
    )
    if report is None:
        return False
    return any(r.confidence_score < LOW_CONFIDENCE_THRESHOLD for r in report.reviews)


async def _run_agent(
    agent: AgentBase,
    ctx: SharedContext,
    redis,
    db_pool,
    budget_manager: ContextBudgetManager | None = None,
    *,
    extra_violation: str | None = None,
) -> bool:
    """Run an agent through its event stream.

    Returns True if the agent emitted a BudgetRequestEvent (caller decides
    whether to invoke CompressionAgent and rerun).
    """
    started = time.perf_counter()
    input_hash = sha256_json(_input_snapshot(ctx, agent.agent_id))
    async with db_pool.acquire() as conn:
        await log_agent_event(
            conn,
            ctx.job_id,
            agent.agent_id,
            "AGENT_START",
            input_hash=input_hash,
        )
    last_output_hash: str | None = None
    last_violations: str | None = None
    budget_requested = False
    streamed_text = ""
    async for event in agent.run(ctx):
        if isinstance(event, AgentEndEvent):
            last_output_hash = event.output_hash or None
            last_violations = event.policy_violations
        elif isinstance(event, BudgetRequestEvent):
            budget_requested = True
        elif isinstance(event, TokenEvent):
            streamed_text += event.text
        await publish_event(redis, ctx.job_id, event.model_dump())
    elapsed_ms = (time.perf_counter() - started) * 1000.0

    final_violations = last_violations
    if extra_violation:
        final_violations = merge_violations(final_violations, extra_violation)

    # E3 post-execution overflow audit: compare the agent's streamed token
    # output to its declared budget. Overflow → append BUDGET_OVERFLOW:<n>.
    overflow_marker: str | None = None
    if budget_manager is not None and streamed_text:
        used = count_tokens(streamed_text)
        budget = budget_manager.budget_for(agent.agent_id)
        if used > budget:
            overflow = used - budget
            budget_manager.report_violation(agent.agent_id, overflow)
            overflow_marker = f"BUDGET_OVERFLOW:{overflow}"
            final_violations = merge_violations(final_violations, overflow_marker)

    async with db_pool.acquire() as conn:
        await log_agent_event(
            conn,
            ctx.job_id,
            agent.agent_id,
            "AGENT_END",
            input_hash=input_hash,
            output_hash=last_output_hash,
            latency_ms=elapsed_ms,
            policy_violations=final_violations,
        )
    return budget_requested


def _input_snapshot(ctx: SharedContext, agent_id: str) -> dict[str, Any]:
    """Per-agent input view: the parts of SharedContext that influence its output."""
    base = {"job_id": ctx.job_id, "query": ctx.query, "agent_id": agent_id}
    if agent_id == "orchestrator":
        return base
    if agent_id == "decomposition":
        return {**base, "routing_plan": ctx.routing_plan.model_dump() if ctx.routing_plan else None}
    if agent_id == "rag":
        return {
            **base,
            "routing_plan": ctx.routing_plan.model_dump() if ctx.routing_plan else None,
            "decomposition": ctx.decomposition.model_dump() if ctx.decomposition else None,
        }
    if agent_id == "synthesis":
        return {
            **base,
            "rag_chunks": [c.chunk_id for c in ctx.rag_chunks],
            "rag_answer_hash": sha256_json(ctx.rag_answer or ""),
            "decomposition": ctx.decomposition.model_dump() if ctx.decomposition else None,
            "resolution_loop_active": ctx.resolution_loop_active,
        }
    if agent_id.startswith("critique:"):
        target = agent_id.split(":", 1)[1]
        return {**base, "target": target}
    return base
