"""ContextBudgetManager — per-agent token budgets and overflow audit.

The token-counting yardstick is the deterministic `len(text) // 4` heuristic.
Anthropic does not ship an offline Python tokenizer; the heuristic is
pessimistic (English averages ~3.5 chars/token for Claude) which is fine for
budget GATING. Replace with a real tokenizer if budgets ever need to be tight.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Mapping

from .settings import settings

_log = logging.getLogger(__name__)


class BudgetExceeded(Exception):
    """Raised by `consume` when an append would push usage over budget."""

    def __init__(self, agent_id: str, requested: int, available: int) -> None:
        super().__init__(
            f"BudgetExceeded(agent={agent_id}, requested={requested}, available={available})"
        )
        self.agent_id = agent_id
        self.requested = requested
        self.available = available


def count_tokens(text: str) -> int:
    """Deterministic, library-free token estimate. See module docstring."""
    if not text:
        return 0
    return len(text) // 4


def default_budgets() -> dict[str, int]:
    return {
        "orchestrator": settings.BUDGET_ORCHESTRATOR,
        "decomposition": settings.BUDGET_DECOMP,
        "rag": settings.BUDGET_RAG,
        "critique": settings.BUDGET_CRITIQUE,
        "synthesis": settings.BUDGET_SYNTHESIS,
        "compression": settings.BUDGET_COMPRESSION,
    }


@dataclass
class ContextBudgetManager:
    agent_budgets: dict[str, int] = field(default_factory=default_budgets)
    agent_usage: dict[str, int] = field(default_factory=dict)
    violations: dict[str, int] = field(default_factory=dict)

    def budget_for(self, agent_id: str) -> int:
        return self.agent_budgets.get(agent_id, settings.MAX_BUDGET_TOKENS)

    def usage_for(self, agent_id: str) -> int:
        return self.agent_usage.get(agent_id, 0)

    def check_budget(self, agent_id: str, tokens_to_add: int) -> bool:
        """Return True if the requested append would stay within budget."""
        budget = self.budget_for(agent_id)
        used = self.usage_for(agent_id)
        return (used + tokens_to_add) <= budget

    def consume(self, agent_id: str, tokens: int) -> None:
        """Record an append. Raises BudgetExceeded if the append overflows."""
        budget = self.budget_for(agent_id)
        used = self.usage_for(agent_id)
        if used + tokens > budget:
            raise BudgetExceeded(agent_id, tokens, budget - used)
        self.agent_usage[agent_id] = used + tokens

    def report_violation(self, agent_id: str, overflow_tokens: int) -> None:
        """Record an audit-level overflow without raising.

        Called by the pipeline's post-execution audit when an agent's committed
        output exceeds its declared budget — meaning it bypassed `check_budget`.
        Multiple violations per agent accumulate into the largest reported value
        (so the worst overflow is the one logged).
        """
        if overflow_tokens <= 0:
            return
        prev = self.violations.get(agent_id, 0)
        self.violations[agent_id] = max(prev, overflow_tokens)
        _log.warning(
            "BUDGET_OVERFLOW agent=%s overflow=%s", agent_id, overflow_tokens
        )

    def violation_for(self, agent_id: str) -> int:
        return self.violations.get(agent_id, 0)


__all__ = [
    "BudgetExceeded",
    "ContextBudgetManager",
    "count_tokens",
    "default_budgets",
]
