---
title: 'Slice E3: ContextBudgetManager + Compression Agent + BUDGET_OVERFLOW detection'
type: 'feature'
created: '2026-05-11'
status: 'in-review'
baseline_commit: 'c23f9f96e1226c775daffb34e57b329ab135c73e'
context:
  - '{project-root}/_bmad-output/planning-artifacts/extended-plan.md'
  - '{project-root}/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Every agent's context appends are currently un-gated. README §Context Window Management requires a `ContextBudgetManager` with per-agent budgets, a `Compression Agent` triggered on `BUDGET_REQUEST`, lossless preservation of tool outputs / citations / scores in `compressed_sidecars`, and a `BUDGET_OVERFLOW` audit on any agent that bypassed the gate.

**Approach:** Add `backend/app/budget.py` with `ContextBudgetManager` (`check_budget` / `consume` / `report_violation`) plus the README-mandated default budgets, configurable via env. Add `backend/app/agents/compression.py` (`CompressionAgent`) that hashes lossless fields, persists them in a new `compressed_sidecars` table (migration 002), and writes a `CompressedContext` onto `SharedContext`. Pipeline instantiates one `ContextBudgetManager` per job, runs the Compression Agent when a `BudgetRequestEvent` is yielded, and runs a post-execution token-count audit after every agent — overflow → `policy_violations = 'BUDGET_OVERFLOW:<overflow>'` on the corresponding `agent_logs` row.

## Boundaries & Constraints

**Always:**
- `ContextBudgetManager.consume()` raises `BudgetExceeded` when the requested `tokens_to_add` would push `agent_usage[agent_id]` above `agent_budgets[agent_id]`.
- `check_budget(agent_id, tokens_to_add)` returns False (does NOT raise) when the addition would overflow; it is the agent's responsibility to react (yield `BudgetRequestEvent` and stop appending).
- Post-execution overflow detection runs after EVERY agent (including critique sub-runs). It uses the same `len(committed_text) // 4` token estimate as the gate to avoid mismatched yardsticks.
- The Compression Agent's **lossless** path covers, at minimum: every entry in `ctx.agent_outputs["tools"]` (per-tool list) and every `ctx.rag_chunks` text. Each goes into `compressed_sidecars` with `field_kind ∈ {"tool_output", "citations"}` and a SHA-256 `field_hash` of the original JSON.
- The Compression Agent's **lossy** path covers `ctx.rag_answer` only: replace it with a one-paragraph summary produced by an LLM call (Anthropic streaming via existing `LLMClient.stream_text`).
- `CompressedContext.compression_ratio = compressed_total_tokens / original_total_tokens` — must be `< 1.0` when compression actually happened.
- The token-counting yardstick is `len(text) // 4` — explicit, deterministic, and matches the gate's accounting. Document this in the budget module.

**Ask First:**
- Retrofitting `check_budget` calls into agents OTHER than `Synthesis`. E3 retrofits Synthesis only (AC1 forces it); Decomposition / RAG / Critique adoption is a follow-up if scope allows.
- Replacing `len(text) // 4` with a real Anthropic tokenizer.

**Never:**
- Do not delete the original `ctx.rag_chunks` after lossless storage — leave them in place but mark `ctx.compressed.lossless_fields_preserved = [...]` so Synthesis knows it can read sidecars by hash.
- Do not invoke Compression more than once per job (idempotent via `ctx.compressed is not None` guard).
- Do not let `BudgetExceeded` propagate to the worker — pipeline catches it, logs `policy_violations='BUDGET_OVERFLOW:<overflow>'` on the `agent_logs` row, and continues the pipeline with the partial answer.
- Do not change the `(SharedContext, LLMClient) -> ToolResult` tool signature or the `AgentBase.run(ctx) -> AsyncIterator[SSEEvent]` agent signature.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Within-budget run | All agents stay under their budgets | No `BudgetRequestEvent` emitted; no `compressed_sidecars` row; no `BUDGET_OVERFLOW`. | N/A |
| Pre-append gate trips | Synthesis check returns False mid-stream | Synthesis yields `BudgetRequestEvent(agent_id='synthesis', tokens_requested=N)`; pipeline pauses, runs CompressionAgent, then Synthesis re-runs with summarized rag_answer | Compression failure → emit `error` event `COMPRESSION_FAILURE`; allow Synthesis to continue without compression |
| Post-execution overflow | Agent ignored the gate; final text > budget | `agent_logs.policy_violations` for that agent ends with `BUDGET_OVERFLOW:<overflow_tokens>` | Audit-only — no pipeline interruption |
| Compression idempotency | `ctx.compressed` already set when another agent trips the gate | CompressionAgent returns early with no new sidecar; pipeline proceeds | N/A |
| Lossless preservation | rag_chunks/tools present at compression time | One row per chunk/tool-entry in `compressed_sidecars` (`field_hash`, `field_kind`, `content`) | DB insert failure → log + degrade to lossy-only |
| Lossy summary | `ctx.rag_answer` non-empty | Replaced by ≤300-char LLM summary; `summary` recorded on `CompressedContext` | LLM error → keep original `rag_answer`; record `compression_ratio=1.0` |
| Compression with empty ctx | No rag_chunks, no tools, no rag_answer | Returns `CompressedContext(compression_ratio=1.0, lossless_fields_preserved=[], summary='')` | No sidecar rows written |

</frozen-after-approval>

## Code Map

- `backend/app/sql/002_compression.sql` — NEW: `compressed_sidecars(id UUID PK, job_id UUID, field_hash TEXT, field_kind TEXT, content JSONB, created_at TIMESTAMPTZ)` + index on `field_hash`. Idempotent via `IF NOT EXISTS`.
- `backend/app/bootstrap.py` — apply `002_compression.sql` between `001_init.sql` and the mega_ro role step (so role grants cover the new table).
- `backend/app/budget.py` — NEW: `ContextBudgetManager`, `BudgetExceeded` exception, `count_tokens(text: str) -> int` helper (`len(text) // 4`).
- `backend/app/settings.py` — add `BUDGET_ORCHESTRATOR=4096`, `BUDGET_DECOMP=3072`, `BUDGET_RAG=6144`, `BUDGET_CRITIQUE=4096`, `BUDGET_SYNTHESIS=8192`, `BUDGET_COMPRESSION=2048`.
- `backend/app/models.py` — add `CompressedContext(compression_ratio, lossless_fields_preserved, summary)`; add `BudgetRequestEvent(type='budget_request', agent_id, tokens_requested)` and append to `SSEEvent` union; add optional `SharedContext.compressed: CompressedContext | None`.
- `backend/app/agents/compression.py` — NEW: `CompressionAgent` with `agent_id='compression'`, `max_context_tokens` from `BUDGET_COMPRESSION`. Iterates `ctx.agent_outputs["tools"]` and `ctx.rag_chunks` → persists sidecars; calls LLM to summarize `ctx.rag_answer`; writes `CompressedContext` onto ctx; yields `agent_start` / `agent_end` only (no token stream needed).
- `backend/app/agents/synthesis.py` — call `manager.check_budget('synthesis', token_count_of_user_msg + 800)` once before streaming. If False, yield `BudgetRequestEvent(...)` and exit early; rely on pipeline's compression-and-rerun loop to retry. After compression, the rerun uses `ctx.compressed.summary` instead of full `ctx.rag_answer` in the user_msg.
- `backend/app/pipeline.py` — instantiate `ContextBudgetManager` per job; on each agent's `BudgetRequestEvent`, run `CompressionAgent` once then re-run the originating agent; after every agent, count tokens of the agent's committed output and call `manager.report_violation(...)` (which records `BUDGET_OVERFLOW:<overflow>` to `agent_logs` via the existing `log_agent_event` policy_violations field).
- `backend/app/persistence.py` — extend `log_agent_event` or add a sidecar helper to append `BUDGET_OVERFLOW:<overflow>` to `policy_violations` (do NOT clobber existing violations — append with `;` separator if non-empty).
- `backend/tests/test_budget_manager.py` — NEW: pure unit tests for `ContextBudgetManager` (`check_budget`, `consume` raises, `report_violation`, idempotency).
- `backend/tests/test_compression_agent.py` — NEW: drive `CompressionAgent.run()` with fixed `SharedContext` containing tools/chunks/rag_answer; assert sidecar rows written, `CompressedContext.compression_ratio < 1.0`, hashes match input.
- `backend/tests/test_pipeline_overflow.py` — NEW: post-execution audit. Use a fake agent that bypasses the gate and produces text > budget; assert the corresponding `agent_logs` row has `policy_violations` ending in `BUDGET_OVERFLOW:<n>`.

## Tasks & Acceptance

**Execution:**
- [x] `backend/app/sql/002_compression.sql` — create `compressed_sidecars` table + index on `field_hash`.
- [x] `backend/app/bootstrap.py` — apply 002 between 001 and the role step.
- [x] `backend/app/settings.py` — add the six `BUDGET_*` fields.
- [x] `backend/app/models.py` — add `CompressedContext`, `BudgetRequestEvent`, extend `SSEEvent` union, add `SharedContext.compressed`.
- [x] `backend/app/budget.py` — `ContextBudgetManager` + `BudgetExceeded` + `count_tokens(text)`.
- [x] `backend/app/agents/compression.py` — `CompressionAgent` with lossless sidecar persistence + lossy LLM summary; computes `compression_ratio`.
- [x] `backend/app/agents/synthesis.py` — pre-stream `check_budget` call; yield `BudgetRequestEvent` and short-circuit if gate trips; on rerun, use `ctx.compressed.summary` in place of full `rag_answer`.
- [x] `backend/app/pipeline.py` — per-job `ContextBudgetManager`; compression-and-rerun loop on `BudgetRequestEvent`; post-execution token-count audit + `BUDGET_OVERFLOW` annotation.
- [x] `backend/app/persistence.py` — extend the agent-log update path to APPEND `BUDGET_OVERFLOW:<n>` without clobbering existing `policy_violations`.
- [x] `backend/tests/test_budget_manager.py` — happy paths + `BudgetExceeded` + `report_violation` + idempotency.
- [x] `backend/tests/test_compression_agent.py` — sidecar row count, `compression_ratio < 1.0`, `summary` populated when `rag_answer` present.
- [x] `backend/tests/test_pipeline_overflow.py` — bypass-the-gate fake agent → `BUDGET_OVERFLOW:<n>` recorded.
- [x] `.env.example` — add the six `BUDGET_*` env vars with their defaults.

**Acceptance Criteria:**
- Given `BUDGET_SYNTHESIS=512`, when a query produces a long rag_answer and Synthesis runs, then Synthesis yields exactly one `BudgetRequestEvent`, the Compression Agent runs once, at least one `compressed_sidecars` row is written, `ctx.compressed.compression_ratio < 1.0`, and the final answer is produced on the rerun (no `EMPTY_OUTPUT` violation).
- Given a fake agent that appends `"x" * 100000` to its committed text and ignores `check_budget`, when the pipeline runs, then its `agent_logs` row's `policy_violations` ends with `BUDGET_OVERFLOW:<n>` where `n >= 1`.
- Given an empty SharedContext (no chunks, no tools, no rag_answer), when `CompressionAgent.run()` is invoked, then `ctx.compressed.compression_ratio == 1.0`, `lossless_fields_preserved == []`, and no `compressed_sidecars` rows are written.
- Given `ctx.compressed` is already set, when a second `BudgetRequestEvent` fires, then `CompressionAgent` returns early — no new sidecars, no new LLM call.
- Given the default `BUDGET_*` env values, when a normal short-query pipeline runs end-to-end, then no `BudgetRequestEvent` fires and no `compressed_sidecars` rows are inserted.

## Design Notes

**Why `len(text) // 4` as the tokenizer:** README §Context Window Management does not specify a tokenizer; Anthropic does not ship an offline Python tokenizer; `tiktoken` is OpenAI-specific. The 4-chars-per-token heuristic is widely-used and good enough for budget gating in this assessment. The estimate is intentionally pessimistic (English averages ~3.5 chars/token for Claude). Replace with a real tokenizer in a future slice if budgets need tighter accuracy.

**Compression as agent vs as service:** Compression is modeled as a real `AgentBase` subclass (yields `agent_start` / `agent_end` SSE events) so the frontend / `/trace` endpoint surfaces it identically to other agents. It does not yield tokens because compression is structural, not conversational.

**The lossless+lossy split:** lossless paths preserve original JSON content under SHA-256 hashes in `compressed_sidecars` so a future audit can reconstruct exactly what the LLM saw. The lossy path is restricted to `rag_answer` because that is the largest free-form prose blob; tool outputs and citation chunks have structured shapes worth keeping verbatim.

**Pipeline rerun discipline:** on `BudgetRequestEvent`, the pipeline calls `CompressionAgent.run()` once, then re-runs the originating agent ONCE more. If the rerun also yields `BudgetRequestEvent`, the pipeline stops re-running (avoids infinite loops) and lets the agent stream whatever it produces — the post-execution audit will catch any actual overflow.

## Verification

**Commands:**
- `docker compose exec api pytest tests/test_budget_manager.py tests/test_compression_agent.py tests/test_pipeline_overflow.py -v` — expected: all green.
- `docker compose exec db psql -U mega -d mega -c "\d compressed_sidecars"` — expected: table with the README-specified columns.
- Smoke: set `BUDGET_SYNTHESIS=512`, submit a long-form question, watch SSE for one `budget_request` then `agent_start{agent_id=compression}` events before the second `agent_start{agent_id=synthesis}`.
