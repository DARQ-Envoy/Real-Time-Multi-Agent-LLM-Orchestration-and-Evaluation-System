"""E3: ContextBudgetManager unit tests."""

from __future__ import annotations

import pytest

from app.budget import BudgetExceeded, ContextBudgetManager, count_tokens


def test_count_tokens_heuristic():
    assert count_tokens("") == 0
    assert count_tokens("abcd") == 1
    assert count_tokens("a" * 400) == 100


def test_check_budget_default_pass():
    m = ContextBudgetManager(agent_budgets={"x": 100})
    assert m.check_budget("x", 50) is True
    assert m.check_budget("x", 100) is True


def test_check_budget_overflow_returns_false():
    m = ContextBudgetManager(agent_budgets={"x": 100})
    assert m.check_budget("x", 101) is False


def test_consume_within_budget():
    m = ContextBudgetManager(agent_budgets={"x": 100})
    m.consume("x", 30)
    m.consume("x", 70)
    assert m.usage_for("x") == 100


def test_consume_overflow_raises():
    m = ContextBudgetManager(agent_budgets={"x": 100})
    m.consume("x", 80)
    with pytest.raises(BudgetExceeded) as excinfo:
        m.consume("x", 30)
    assert excinfo.value.agent_id == "x"
    assert excinfo.value.requested == 30


def test_report_violation_records_max():
    m = ContextBudgetManager(agent_budgets={"x": 100})
    m.report_violation("x", 5)
    m.report_violation("x", 12)
    m.report_violation("x", 3)
    assert m.violation_for("x") == 12  # max wins


def test_report_violation_ignores_non_positive():
    m = ContextBudgetManager(agent_budgets={"x": 100})
    m.report_violation("x", 0)
    m.report_violation("x", -5)
    assert m.violation_for("x") == 0


def test_unknown_agent_falls_back_to_max_budget(monkeypatch):
    from app.settings import settings

    monkeypatch.setattr(settings, "MAX_BUDGET_TOKENS", 9999)
    m = ContextBudgetManager(agent_budgets={})
    assert m.budget_for("unknown") == 9999
