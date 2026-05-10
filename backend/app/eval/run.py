"""Eval runner: invokes pipeline.run() per case in-process and persists rows."""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from typing import Any

from arq import create_pool as create_arq_pool
from arq.connections import RedisSettings

from .. import pipeline
from ..db import create_pool as create_db_pool
from ..llm import LLMClient
from ..models import SharedContext
from ..persistence import sha256_json
from ..settings import settings
from . import scorer
from .cases import CASES, EvalCase

DIMENSIONS = ("answer_correctness", "citation_accuracy", "critique_agreement")
PASS_THRESHOLD = 0.6


def _redis_settings() -> RedisSettings:
    return RedisSettings.from_dsn(settings.REDIS_URL)


def _flatten_actual_chunk_ids(final_answer) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for sp in final_answer:
        for cid in sp.source_chunk_ids:
            if cid not in seen:
                seen.add(cid)
                ordered.append(cid)
    return ordered


async def _run_one_case(
    case: EvalCase,
    db_pool,
    arq_pool,
    llm: LLMClient,
    sem: asyncio.Semaphore,
) -> dict[str, Any]:
    async with sem:
        job_id = uuid.uuid4()
        async with db_pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO jobs (id, query, status) VALUES ($1, $2, $3)",
                job_id,
                case.query,
                "RUNNING",
            )
        ctx = SharedContext(job_id=str(job_id), query=case.query)
        pipeline_error: str | None = None
        try:
            final_answer = await pipeline.run(ctx, arq_pool, db_pool)
        except Exception as exc:
            pipeline_error = f"{exc.__class__.__name__}: {exc}"
            final_answer = []

        actual_text = "\n".join(p.sentence_text for p in final_answer)
        actual_chunk_ids = _flatten_actual_chunk_ids(final_answer)

        ac_score, ac_just = await scorer.answer_correctness(
            actual_text, case.expected_answer_summary, llm
        )
        cit_score = scorer.citation_accuracy(actual_chunk_ids, case.expected_chunk_ids)
        crit_score = scorer.critique_agreement(ctx.critique_reports)

        scores = {
            "answer_correctness": round(ac_score, 4),
            "citation_accuracy": round(cit_score, 4),
            "critique_agreement": round(crit_score, 4),
        }
        passed = all(v >= PASS_THRESHOLD for v in scores.values())

        async with db_pool.acquire() as conn:
            tool_call_rows = await conn.fetch(
                """
                SELECT tool_name, success, error_code, accepted, retry_number,
                       latency_ms
                  FROM tool_calls
                 WHERE job_id = $1
                 ORDER BY id
                """,
                job_id,
            )
            await conn.execute(
                "UPDATE jobs SET status = $1, completed_at = now() WHERE id = $2",
                "COMPLETE" if final_answer else "FAILED",
                job_id,
            )

        agent_prompts = {
            "query": case.query,
            "agent_sequence": list(ctx.routing_plan.agent_sequence)
            if ctx.routing_plan
            else [],
        }
        agent_outputs = {
            "routing_plan": ctx.routing_plan.model_dump() if ctx.routing_plan else None,
            "decomposition": ctx.decomposition.model_dump()
            if ctx.decomposition
            else None,
            "rag_chunk_ids": [c.chunk_id for c in ctx.rag_chunks],
            "rag_answer_excerpt": (ctx.rag_answer or "")[:400],
            "final_answer": [p.model_dump() for p in final_answer],
            "critique_reports": [cr.model_dump() for cr in ctx.critique_reports],
            "pipeline_error": pipeline_error,
            "judge_justification": ac_just,
        }
        tool_calls_serialised = [dict(r) for r in tool_call_rows]

        return {
            "case_id": case.case_id,
            "category": case.category,
            "query": case.query,
            "agent_prompts": agent_prompts,
            "tool_calls": tool_calls_serialised,
            "agent_outputs": agent_outputs,
            "scores": scores,
            "passed": passed,
        }


def _summary(case_results: list[dict[str, Any]]) -> dict[str, Any]:
    by_category: dict[str, dict[str, list[float]]] = {}
    overall: dict[str, list[float]] = {d: [] for d in DIMENSIONS}
    for r in case_results:
        cat = r["category"]
        bucket = by_category.setdefault(cat, {d: [] for d in DIMENSIONS})
        for d in DIMENSIONS:
            v = float(r["scores"][d])
            bucket[d].append(v)
            overall[d].append(v)

    def avg(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    by_category_avg = {
        cat: {d: avg(vs[d]) for d in DIMENSIONS} for cat, vs in by_category.items()
    }
    overall_avg = {d: avg(overall[d]) for d in DIMENSIONS}
    return {
        "by_category": by_category_avg,
        "overall": overall_avg,
        "case_count": len(case_results),
        "passed_count": sum(1 for r in case_results if r["passed"]),
    }


def _print_table(summary: dict[str, Any]) -> None:
    headers = ["Category", *DIMENSIONS]
    rows: list[list[str]] = [headers]
    for cat in ("baseline", "ambiguous", "adversarial"):
        if cat not in summary["by_category"]:
            continue
        row = [cat]
        for d in DIMENSIONS:
            row.append(f"{summary['by_category'][cat][d]:.2f}")
        rows.append(row)
    rows.append(["OVERALL", *(f"{summary['overall'][d]:.2f}" for d in DIMENSIONS)])

    widths = [max(len(r[i]) for r in rows) for i in range(len(headers))]
    sep = "-+-".join("-" * w for w in widths)
    for i, r in enumerate(rows):
        line = " | ".join(s.ljust(widths[j]) for j, s in enumerate(r))
        print(line)
        if i == 0:
            print(sep)


async def run_eval(dry_run: bool = False) -> int:
    if dry_run:
        print(f"Eval harness — {len(CASES)} cases, scorers: {', '.join(DIMENSIONS)}")
        for c in CASES:
            print(f"  [{c.category}] {c.case_id} — {c.query!r}")
        return 0

    llm = LLMClient()
    if not llm.configured:
        print("LLM_NOT_CONFIGURED: set ANTHROPIC_API_KEY before running eval.", file=sys.stderr)
        return 2

    db_pool = await create_db_pool()
    arq_pool = await create_arq_pool(_redis_settings())
    sem = asyncio.Semaphore(settings.EVAL_CONCURRENCY)

    started = time.perf_counter()
    try:
        case_results = await asyncio.gather(
            *[_run_one_case(c, db_pool, arq_pool, llm, sem) for c in CASES]
        )

        summary = _summary(case_results)
        run_id = uuid.uuid4()
        run_hash = sha256_json(sorted([(c.case_id, c.query) for c in CASES])) or ""

        async with db_pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO eval_runs (id, run_hash, summary)
                VALUES ($1, $2, $3::jsonb)
                """,
                run_id,
                run_hash,
                json.dumps(summary),
            )
            for r in case_results:
                await conn.execute(
                    """
                    INSERT INTO eval_cases
                        (run_id, category, query, agent_prompts, tool_calls,
                         agent_outputs, scores, passed)
                    VALUES ($1, $2, $3, $4::jsonb, $5::jsonb, $6::jsonb,
                            $7::jsonb, $8)
                    """,
                    run_id,
                    r["category"],
                    r["query"],
                    json.dumps(r["agent_prompts"], default=str),
                    json.dumps(r["tool_calls"], default=str),
                    json.dumps(r["agent_outputs"], default=str),
                    json.dumps(r["scores"]),
                    r["passed"],
                )
    finally:
        await arq_pool.aclose()
        await db_pool.close()

    elapsed_s = time.perf_counter() - started
    _print_table(summary)
    print()
    print(
        f"run_id={run_id} cases={summary['case_count']} "
        f"passed={summary['passed_count']} elapsed={elapsed_s:.1f}s"
    )
    overall_pass = all(summary["overall"][d] >= PASS_THRESHOLD for d in DIMENSIONS)
    return 0 if overall_pass else 1
