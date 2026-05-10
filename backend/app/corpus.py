"""Hardcoded RAG corpus: 8 chunks summarised from README.md.

Slice 1-2.5: keyword retrieval only. No vectors, no embeddings. The chunks
are deliberately short and topic-tagged so the keyword scorer can find them.
"""

from __future__ import annotations

import re

CORPUS: list[dict[str, str]] = [
    {
        "chunk_id": "c1-retry",
        "source_url": "README.md#retry-policy",
        "text": (
            "All tools support up to two retries. Each retry is logged as a separate "
            "ToolCall record with retry_number and a retry_reason string. An agent "
            "that receives a tool result and deems it insufficient must log "
            "accepted_by_agent=false before requesting a retry. After two retries, "
            "the fallback contract for that tool activates unconditionally."
        ),
    },
    {
        "chunk_id": "c2-agent-boundaries",
        "source_url": "README.md#agent-descriptions--decision-boundaries",
        "text": (
            "The Orchestrator is the single mediator for all agent activity. It "
            "routes queries to sub-agents, defines execution order, and allocates "
            "context budgets. It will not skip the Critique Agent regardless of "
            "confidence. It will not call sub-agents in parallel when a dependency "
            "edge exists between them. Every agent has a strict decision boundary."
        ),
    },
    {
        "chunk_id": "c3-schema",
        "source_url": "README.md#database-schema",
        "text": (
            "The PostgreSQL schema includes seven tables: jobs (id, query, status, "
            "final_answer, routing_plan), agent_logs (per-event structured logs with "
            "input_hash and output_hash), tool_calls (tool result records with "
            "retry_number), eval_runs and eval_cases (eval infrastructure), "
            "prompt_rewrites (self-improvement loop), and performance_deltas "
            "(before/after re-eval scores)."
        ),
    },
    {
        "chunk_id": "c4-eval-pipeline",
        "source_url": "README.md#evaluation-pipeline",
        "text": (
            "The eval pipeline scores 15 test cases across 6 dimensions: "
            "answer_correctness, citation_accuracy, contradiction_resolution, "
            "tool_efficiency, budget_compliance, and critique_agreement. Each case "
            "stores agent prompts, tool calls, agent outputs, scores, and a run_hash. "
            "Re-running on identical inputs produces a new eval_run row, enabling "
            "per-dimension deltas across runs."
        ),
    },
    {
        "chunk_id": "c5-streaming",
        "source_url": "README.md#streaming--observability",
        "text": (
            "SSE stream events have a type field: agent_start, token (one event per "
            "token), tool_call_start, tool_call_end, budget_update, agent_end, "
            "job_complete, and error. Logs are queryable via the Datasette interface "
            "on port 8080 and via GET /trace/{job_id}. The structured log schema "
            "captures timestamp, job_id, agent_id, event_type, input_hash, "
            "output_hash, latency_ms, token_count, and policy_violations."
        ),
    },
    {
        "chunk_id": "c6-self-improvement",
        "source_url": "README.md#self-improving-prompt-loop",
        "text": (
            "After an eval run completes, the Meta-Agent ranks prompts by their "
            "worst-scoring dimension and produces a PromptRewrite stored as PENDING. "
            "A human approves or rejects the rewrite via POST /rewrites/{id}/approve "
            "or /reject. Approved rewrites are applied and a re-eval runs on "
            "previously failed cases, persisting a PerformanceDelta with before, "
            "after, and delta scores."
        ),
    },
    {
        "chunk_id": "c7-tool-catalogue",
        "source_url": "README.md#tool-catalogue",
        "text": (
            "The four tools are web_search (up to 10 results, fallback to "
            "self_reflection on TIMEOUT), code_exec (subprocess sandbox, no network, "
            "restricted filesystem, fallback to decomposition reformulation on "
            "EXEC_ERROR), sql_lookup (NL to SQL via LLM, schema-aware, retried once "
            "with simplified hint on MALFORMED), and self_reflection (compares the "
            "agent's own previous outputs from SharedContext, advisory only)."
        ),
    },
    {
        "chunk_id": "c8-context-budget",
        "source_url": "README.md#context-window-management",
        "text": (
            "The ContextBudgetManager tracks per-agent token budgets and usage. "
            "Every agent declares MAX_CONTEXT_TOKENS and calls check_budget before "
            "appending context. On overflow, the Orchestrator invokes the "
            "Compression Agent which returns a CompressedContext. Default budgets: "
            "Orchestrator 4096, Decomposition 3072, RAG 6144, Critique 4096, "
            "Synthesis 8192, Compression 2048."
        ),
    },
]


def _tokens(text: str) -> set[str]:
    return set(re.findall(r"\w+", text.lower()))


def keyword_search(query: str, k: int = 3) -> list[dict[str, str]]:
    """Return top-k chunks by Jaccard-like overlap with the query.

    Score = |query_tokens ∩ chunk_tokens| / |query_tokens|.
    Ties broken by chunk_id ascending. Chunks with score 0 are dropped.
    """
    q = _tokens(query)
    if not q:
        return []
    scored: list[tuple[float, str, dict[str, str]]] = []
    for chunk in CORPUS:
        c = _tokens(chunk["text"])
        score = len(q & c) / len(q)
        scored.append((score, chunk["chunk_id"], chunk))
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [{**c, "relevance_score": s} for s, _id, c in scored[:k] if s > 0]
