"""E3: post-execution BUDGET_OVERFLOW audit on agents that bypass check_budget.

Exercises pipeline._run_agent against a fake agent that streams >budget tokens
without consulting the manager — the audit must append BUDGET_OVERFLOW:<n> to
the AGENT_END agent_logs row.
"""

from __future__ import annotations

from typing import AsyncIterator

import pytest

from app import pipeline
from app.budget import ContextBudgetManager
from app.models import AgentEndEvent, AgentStartEvent, SSEEvent, TokenEvent
from app.agents.base import AgentBase


class _OverflowAgent(AgentBase):
    agent_id = "synthesis"

    async def run(self, ctx) -> AsyncIterator[SSEEvent]:
        yield AgentStartEvent(agent_id=self.agent_id, budget_remaining=100)
        # 100k chars / 4 = 25k tokens — well above any default budget.
        yield TokenEvent(agent_id=self.agent_id, text="x" * 100_000)
        yield AgentEndEvent(
            agent_id=self.agent_id, output_hash="deadbeef", policy_violations=None
        )


def _last_violation(pool) -> str | None:
    for sql, args in reversed(pool.executed):
        if "INSERT INTO agent_logs" not in sql:
            continue
        # log_agent_event positional args:
        # (job_id, agent_id, event_type, input_hash, output_hash, latency_ms,
        #  token_count, policy_violations)
        if args[2] == "AGENT_END":
            return args[7]
    return None


async def test_overflow_recorded_on_bypass(shared_ctx, fake_db_pool, fake_redis):
    manager = ContextBudgetManager(agent_budgets={"synthesis": 100})
    agent = _OverflowAgent()
    await pipeline._run_agent(
        agent, shared_ctx, fake_redis, fake_db_pool, manager
    )
    violations = _last_violation(fake_db_pool)
    assert violations is not None
    assert "BUDGET_OVERFLOW:" in violations
    n = int(violations.split("BUDGET_OVERFLOW:")[1].split(";")[0])
    assert n >= 1
    # Manager also tracks the max overflow.
    assert manager.violation_for("synthesis") >= 1


async def test_no_overflow_within_budget(shared_ctx, fake_db_pool, fake_redis):
    class _SmallAgent(_OverflowAgent):
        async def run(self, ctx):
            yield AgentStartEvent(agent_id=self.agent_id, budget_remaining=100)
            yield TokenEvent(agent_id=self.agent_id, text="ok")
            yield AgentEndEvent(
                agent_id=self.agent_id,
                output_hash="abc",
                policy_violations=None,
            )

    manager = ContextBudgetManager(agent_budgets={"synthesis": 100})
    await pipeline._run_agent(
        _SmallAgent(), shared_ctx, fake_redis, fake_db_pool, manager
    )
    violations = _last_violation(fake_db_pool)
    assert violations is None
    assert manager.violation_for("synthesis") == 0


async def test_overflow_appended_to_existing_violations(
    shared_ctx, fake_db_pool, fake_redis
):
    class _OverflowWithPriorViolation(_OverflowAgent):
        async def run(self, ctx):
            yield AgentStartEvent(agent_id=self.agent_id, budget_remaining=100)
            yield TokenEvent(agent_id=self.agent_id, text="x" * 100_000)
            yield AgentEndEvent(
                agent_id=self.agent_id,
                output_hash="abc",
                policy_violations="FALLBACK_TO_RAG_DRAFT",
            )

    manager = ContextBudgetManager(agent_budgets={"synthesis": 100})
    await pipeline._run_agent(
        _OverflowWithPriorViolation(),
        shared_ctx,
        fake_redis,
        fake_db_pool,
        manager,
    )
    violations = _last_violation(fake_db_pool)
    assert violations is not None
    assert violations.startswith("FALLBACK_TO_RAG_DRAFT;BUDGET_OVERFLOW:")
