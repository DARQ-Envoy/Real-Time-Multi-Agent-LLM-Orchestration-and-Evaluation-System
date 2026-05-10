"""Three scoring functions for the eval harness.

answer_correctness  — LLM-as-judge via tool-use; one LLM call per case.
citation_accuracy   — Jaccard on chunk_id sets; pure Python.
critique_agreement  — fraction of synthesis sentences with verdict=SUPPORTED.

Other README dimensions (contradiction_resolution, tool_efficiency,
budget_compliance) are intentionally skipped per plan.md.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from ..llm import LLMClient
from ..models import CritiqueReport

SCORE_ANSWER_TOOL: dict[str, Any] = {
    "name": "score_answer",
    "description": (
        "Score how well the actual answer matches the expected summary, "
        "0.0 (unrelated/wrong) to 1.0 (semantically equivalent)."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "score": {"type": "number", "minimum": 0, "maximum": 1},
            "justification": {"type": "string"},
        },
        "required": ["score", "justification"],
    },
}

JUDGE_SYSTEM = (
    "You are an evaluator. You compare an actual answer to an expected "
    "summary. Score by semantic similarity, not by surface wording. "
    "Always call the score_answer tool. Be strict but fair: partial "
    "matches deserve partial credit."
)


async def answer_correctness(
    actual_text: str,
    expected_summary: str,
    llm: LLMClient,
) -> tuple[float, str]:
    if not actual_text.strip():
        return 0.0, "Actual answer was empty."
    user_msg = (
        f"Expected summary:\n{expected_summary}\n\n"
        f"Actual answer:\n{actual_text}\n\n"
        "Score now via the score_answer tool."
    )
    try:
        result = await llm.call_tool(
            system=JUDGE_SYSTEM,
            user=user_msg,
            tool=SCORE_ANSWER_TOOL,
            max_tokens=300,
        )
    except Exception as exc:
        return 0.0, f"Judge call failed: {exc.__class__.__name__}"
    raw_score = result.get("score")
    try:
        score = max(0.0, min(1.0, float(raw_score)))
    except (TypeError, ValueError):
        return 0.0, "Judge returned non-numeric score."
    justification = str(result.get("justification") or "").strip()
    return score, justification


def citation_accuracy(
    actual_chunk_ids: Iterable[str],
    expected_chunk_ids: Iterable[str],
) -> float:
    actual = set(actual_chunk_ids)
    expected = set(expected_chunk_ids)
    if not actual and not expected:
        return 1.0
    if not actual or not expected:
        return 0.0
    return len(actual & expected) / len(actual | expected)


def critique_agreement(critique_reports: list[CritiqueReport]) -> float:
    synth = next(
        (cr for cr in reversed(critique_reports) if cr.target_agent_id == "synthesis"),
        None,
    )
    if synth is None or not synth.reviews:
        return 0.0
    supported = sum(1 for r in synth.reviews if r.verdict == "SUPPORTED")
    return supported / len(synth.reviews)
