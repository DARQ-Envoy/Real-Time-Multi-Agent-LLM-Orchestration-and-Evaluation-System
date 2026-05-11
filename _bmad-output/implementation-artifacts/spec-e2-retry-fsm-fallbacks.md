---
title: 'Slice E2: 2-retry FSM + retry_reason + per-tool fallback contracts'
type: 'feature'
created: '2026-05-11'
status: 'done'
baseline_commit: '86c9af9d85d3208e41f61ce588c3db5e9554daa2'
context:
  - '{project-root}/_bmad-output/planning-artifacts/extended-plan.md'
  - '{project-root}/README.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-e1-full-tool-layer.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** E1 wired four tools through `run_with_retry(..., max_retries=1)`. README §Tool Catalogue requires TWO retries per tool with a `retry_reason` logged, AND four per-tool fallback contracts (verbatim in extended-plan §E2). Neither is in place. `tool_calls` table has no `retry_reason` column, and there is no fallback registry.

**Approach:** Add a `tool_calls.retry_reason` column via migration 004; bump `MAX_RETRIES_DEFAULT` to 2 and have the runner derive a `retry_reason` from the prior attempt's outcome before each retry; route the terminal failure of an exhausted retry chain (and one early-retry hook for `sql_lookup`) through a `FALLBACK_REGISTRY` keyed by `(tool_name, error_code)`. Introduce `tools/retry.py` as the new home for the retry function; `tools/runner.py` becomes a thin back-compat re-export.

## Boundaries & Constraints

**Always:**
- Every retried `tool_calls` row (i.e. `retry_number > 0`) MUST have a non-null `retry_reason`. The first attempt (`retry_number=0`) MUST have NULL `retry_reason` (never a retry by definition).
- Fallbacks fire EXACTLY ONCE per `(job_id, tool_name)`. The runner returns immediately after fallback completes — no further retries.
- `accepted_by_agent` is set to `False` on every failed/empty attempt BEFORE the retry decision (this is what triggers retry). Preserve the existing `_accept()` heuristic.
- The `web_search` fallback re-uses the **existing** `self_reflection` tool via the same `run_with_retry` path (so its `tool_calls` row is recorded normally with `tool_name='self_reflection'`).
- `sql_lookup` early-fallback (MALFORMED on retry 1) injects a simplified schema hint into `ctx.agent_outputs["__retry_hint__"] = "schema_simplified"` BEFORE the next attempt; the tool reads it and asks the LLM for a simpler query. On terminal MALFORMED after retry 2, log `SQL_FALLBACK_SKIPPED` and do not write any data fields on SharedContext.
- All four fallback events use distinct names: `WEB_FALLBACK`, `TOOL_FAILURE`, `SQL_FALLBACK_SKIPPED`, `SELF_REFLECTION_FAILED`. They are emitted as SSE `error` events with the `error_code` set to that name (re-using the existing `ErrorEvent` shape).

**Ask First:**
- Any change to the `_accept()` heuristic in `runner.py`/`retry.py`. The README's "accepted_by_agent=False is the retry trigger" guarantee depends on it.
- Whether `code_exec`'s "ask DecompositionAgent to reformulate" should literally re-invoke Decomposition mid-pipeline (writing `ctx.routing_plan` and re-running) — OR just log `TOOL_FAILURE`, store the suggestion on ctx, and continue (no re-execution). The MVP-friendly reading is the latter; flag if you choose otherwise.

**Never:**
- Do not introduce a `ContextBudgetManager` gate or budget update on fallbacks — that is Slice E3.
- Do not delete `tools/runner.py`. Keep it as a re-export (`from .retry import *`) so any external import path stays valid.
- Do not change the `(SharedContext, LLMClient) -> ToolResult` tool signature.
- Do not retry on `success=True AND accepted=True` results (only `_accept()=False` attempts retry).
- Do not double-invoke a fallback if it was already triggered for the same `(job_id, tool_name)`.
- Do not let a fallback exception propagate — wrap it; emit an `error` event with code `FALLBACK_FAILURE` and continue.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Happy retry-0 success | tool returns `success=True, data=...` on attempt 0 | One `tool_calls` row (`retry_number=0`, `retry_reason=NULL`, `accepted=True`); no fallback | N/A |
| 1-retry recovery | attempt 0 TIMEOUT → attempt 1 success | Two rows; row 1 has `retry_number=1`, `retry_reason='timeout'`, `success=True`; no fallback | N/A |
| `web_search` 2-retry exhaustion | all 3 attempts TIMEOUT | 3 rows for web_search (`retry_number` 0/1/2; `retry_reason` NULL/timeout/timeout); 1 row for self_reflection fallback; `ctx.agent_outputs["web_unavailable"]=True`; SSE `error` event `WEB_FALLBACK` | swallow `self_reflection` exceptions → `FALLBACK_FAILURE` |
| `code_exec` 2-retry EXEC_ERROR | all 3 attempts EXEC_ERROR | 3 rows; SSE `error` event `TOOL_FAILURE`; `ctx.agent_outputs["code_exec_failed"]={"reason": <stderr>, "suggested_replan": True}`; pipeline continues with existing `RoutingPlan` (no mid-pipeline re-execution) | swallow exception → `FALLBACK_FAILURE` |
| `sql_lookup` MALFORMED on retry 1 then success | attempt 0 MALFORMED → schema-hint injected → attempt 1 success | 2 rows; row 1 has `retry_reason='malformed_schema_hint_injected'`; `success=True` on row 1; no fallback (early-recovery worked) | N/A |
| `sql_lookup` MALFORMED through retry 2 | all 3 MALFORMED (even with hint) | 3 rows; SSE `error` event `SQL_FALLBACK_SKIPPED`; no data field set on ctx | N/A |
| `self_reflection` EXEC_ERROR | the tool itself raises | 1 row `retry_number=0` (advisory tool: do NOT retry); SSE `error` event `SELF_REFLECTION_FAILED`; pipeline continues | swallow |
| Fallback re-invocation guard | run_with_retry called twice for same (job, tool) after first chain exhausted+fell-back | Second call's first attempt runs normally; if it also exhausts, fallback fires ONCE more (per-call, not per-job). The guard is "no extra retries after the same chain's fallback fires" (AC4). | — |

</frozen-after-approval>

## Code Map

- `backend/app/tools/runner.py` — currently owns `run_with_retry`; becomes a one-line re-export: `from .retry import *`.
- `backend/app/tools/retry.py` — NEW. Owns the retry FSM. Same function name `run_with_retry` (back-compat). Logic: loop `max_retries+1` attempts; compute `retry_reason` from prior attempt (`error_code.lower()` or `"not_accepted"`); on `sql_lookup` MALFORMED at `retry_number==0` insert hint into ctx; on chain exhaustion call `fallbacks.dispatch(...)`. Tracks fired fallbacks in `ctx.agent_outputs["__fallbacks_fired__"]` so AC4 holds.
- `backend/app/tools/fallbacks.py` — NEW. `FALLBACK_REGISTRY: dict[tuple[str, str], Callable]`. Keys: `("web_search","TIMEOUT")`, `("code_exec","EXEC_ERROR")`, `("sql_lookup","MALFORMED")`, `("self_reflection","EXEC_ERROR")`. Each fallback is `async def(ctx, llm, db_pool, redis, terminal_result) -> None` that emits events and mutates ctx per the contract table above. Wrap each in try/except → `FALLBACK_FAILURE` event.
- `backend/app/sql/004_tool_calls_retry_reason.sql` — NEW. `ALTER TABLE tool_calls ADD COLUMN IF NOT EXISTS retry_reason TEXT;` (idempotent).
- `backend/app/bootstrap.py` — apply `004_*.sql` after the grants step. Idempotent.
- `backend/app/tools/sql_lookup.py` — read `ctx.agent_outputs.get("__retry_hint__")`; if `"schema_simplified"`, swap the schema text passed to the LLM with a compact form (table names only, no column types) and append "Return the simplest correct SELECT for this question." to the user message.
- `backend/app/pipeline.py` — no logic change to `_dispatch_tool_calls` (still calls `run_with_retry`). Add: after the agent loop, if `ctx.agent_outputs.get("web_unavailable")` is True, annotate the final answer's metadata with `WEB_UNAVAILABLE` (e.g. on the synthesis pass via SharedContext flag).
- `backend/app/agents/synthesis.py` — when emitting final answer, if `ctx.agent_outputs.get("web_unavailable")` is True, prepend `[WEB_UNAVAILABLE]` to the answer text OR include it in the AnswerSegment metadata (whichever fits the existing model — pick the lowest-blast-radius spot).
- `backend/app/models.py` — no schema change. (`SharedContext.agent_outputs` is already an open dict.)
- `backend/tests/test_retry_fsm.py` — NEW: covers all 8 I/O Matrix rows. Forced-failure via monkeypatched tool fn returning crafted `ToolResult`s. Asserts row counts, `retry_reason` values, fallback events, idempotency.
- `backend/tests/test_fallbacks.py` — NEW: unit-tests each fallback callable's side effects (ctx flag set, SSE event emitted, exceptions swallowed).
- `backend/tests/test_tool_sql_lookup.py` — extend: add a test forcing first MALFORMED, then verifying the hint is injected on the second LLM call (use a FakeLLM that returns malformed SQL on call 1 and valid SQL on call 2).
- `backend/tests/conftest.py` — extend FakeLLM (or add `ScriptedLLM`) to script multi-call sequences.

## Tasks & Acceptance

**Execution:**
- [x] `backend/app/sql/004_tool_calls_retry_reason.sql` — add column, idempotent.
- [x] `backend/app/bootstrap.py` — apply 004 after the grants.
- [x] `backend/app/tools/retry.py` — implement the FSM: 2 retries, `retry_reason` per row, sql_lookup early-hint injection, fallback dispatch on exhaustion, per-chain idempotency via `__fallbacks_fired__`.
- [x] `backend/app/tools/runner.py` — collapse to `from .retry import *  # back-compat`.
- [x] `backend/app/tools/fallbacks.py` — `FALLBACK_REGISTRY` + 4 async callables; wrap each in try/except emitting `FALLBACK_FAILURE`.
- [x] `backend/app/tools/sql_lookup.py` — consume `__retry_hint__`; build a compact-schema prompt when hint is `"schema_simplified"`.
- [x] `backend/app/agents/synthesis.py` — propagate `web_unavailable` flag onto the final answer's first segment (text prefix or metadata, lowest-blast-radius).
- [x] `backend/app/agents/decomposition.py` — minor: if `ctx.agent_outputs.get("code_exec_failed")` is set on entry, prepend a sentence to its user prompt ("Avoid plans that require code execution.") so a future Decomp pass can react. Pure additive read; no signature change.
- [ ] Update INSERT in `retry.py` to write `retry_reason` (new column).
- [x] `backend/tests/test_retry_fsm.py` — 8 I/O Matrix rows.
- [x] `backend/tests/test_fallbacks.py` — per-fallback unit tests.
- [x] `backend/tests/test_tool_sql_lookup.py` — extend with hint-injection test.
- [x] `backend/tests/conftest.py` — scripted LLM helper.

**Acceptance Criteria:**
- Given a `web_search` tool that always TIMEOUTs, when the pipeline runs, then exactly 3 `tool_calls` rows for `web_search` exist (`retry_number` ∈ {0,1,2}; `retry_reason` NULL/timeout/timeout); exactly 1 row for `self_reflection` follows; SSE includes a `WEB_FALLBACK` `error` event; the final answer carries the `WEB_UNAVAILABLE` marker.
- Given any retried `tool_calls` row (i.e. `retry_number > 0`), then `retry_reason` is non-null.
- Given a tool returning `success=True` with a non-empty `data`, when run_with_retry runs, then `accepted_by_agent=True` and no retry fires.
- Given a forced 3rd-call fallback that completes, when `run_with_retry` is called a second time for the same `(job_id, tool_name)` and immediately succeeds on attempt 0, then no new fallback fires (fallback ran ZERO additional times — only retry-chain exhaustion triggers it).
- Given `sql_lookup` returning MALFORMED on attempt 0 followed by VALID SQL on attempt 1, when the pipeline runs, then the second LLM call's system prompt contains the compact-schema hint phrase; only 2 rows are inserted; no `SQL_FALLBACK_SKIPPED` event fires.
- Given `self_reflection` raising EXEC_ERROR, when the pipeline runs, then exactly 1 `tool_calls` row exists for it (no retries) and a `SELF_REFLECTION_FAILED` SSE event fires; pipeline continues.

## Spec Change Log

### 2026-05-11 — Patch loop after step-04 review (iteration 1)

Three review agents ran in parallel. No findings rose to **intent_gap** or **bad_spec** triggering revert. Seven patches applied in-place; spec wording clarification recorded; remaining items appended to `deferred-work.md`.

**Patches applied:**
1. `backend/app/tools/retry.py` — `tool_fn` exceptions are now caught and synthesized into a `ToolResult(success=False, error_code="EXEC_ERROR")`. Without this, a tool that crashed (instead of returning a failed result) tanked the entire job mid-loop with no row written and no fallback dispatched. The hint cleanup at chain end + accepted-path also prevented hint leakage to subsequent calls.
2. `backend/app/tools/retry.py` — `publish_event` and the `tool_calls` INSERT are wrapped in `_safe_publish` / `_safe_insert_row` helpers that log a warning and continue. A transient Redis or DB blip no longer kills the FSM mid-attempt.
3. `backend/app/tools/retry.py` — when `__retry_hint__` is still active on attempt 2, the new row's `retry_reason` stays `"malformed_schema_hint_injected"` rather than the bare `"malformed"`. This keeps the audit honest: a forensic reader can see at-a-glance which attempts ran with the compact-schema prompt.
4. `backend/app/tools/retry.py` — the `assert next_retry_reason in _RETRY_REASONS` was strippable under `python -O`; replaced with a guarded fallback to `"not_accepted"`. Defense against silent corruption in optimized builds.
5. `backend/app/agents/synthesis.py` — `WEB_UNAVAILABLE` is now PREPENDED as a separate `SentenceProvenance` segment (source_agent="web_fallback") rather than mutating `provenance[0].sentence_text`. The mutation broke determinism: streamed `TokenEvent`s never carried the prefix, but `output_hash` was computed AFTER the mutation, so any consumer hashing the stream saw divergence from the persisted answer. Separate-segment approach is also idempotent (guard on `[WEB_UNAVAILABLE]` head).
6. `backend/app/tools/retry.py` + `backend/app/pipeline.py` — `run_with_retry` now accepts an optional `input_payload` parameter; the pipeline passes `{agent_id, tool_name, input: dict(planned.input)}`. The `tool_calls.input` column now records the real planned input instead of the `{job_id, query_hash}` placeholder. Resolves an item that was deferred from E1.
7. `backend/app/tools/retry.py` — docstring comment about "runs even when max_retries=0" rephrased to match reality: the fallback registry is keyed by `(tool_name, error_code)`, so only registered combinations actually run (e.g. `self_reflection` fires SELF_REFLECTION_FAILED only on EXEC_ERROR, not TIMEOUT).

**Spec wording resolution (no code change):**
- The Boundary statement "Fallbacks fire EXACTLY ONCE per `(job_id, tool_name)`" conflicts with the I/O Matrix row "fallback fires ONCE more (per-call, not per-job)". The implementation matches the **per-call** reading (each `run_with_retry` chain that exhausts may fire one fallback). The Boundary statement should have been "EXACTLY ONCE per exhausted chain". Recorded here; no `__fallbacks_fired__` job-scoped guard was added.

**KEEP (preserved across the patch loop):**
- 2-retry default with `max_retries=0` override for self_reflection (advisory).
- The schema-hint mechanism via `ctx.agent_outputs["__retry_hint__"]` — survives nested call paths because retry.py clears it on accept and on chain exhaustion.
- The fallback registry's lazy import from `retry.py` (kept function-scoped to prevent module-level circular import).
- `_accept` heuristic unchanged.

## Design Notes

**Why introduce `retry.py` and keep `runner.py` as a re-export:** the extended-plan literally names `tools/retry.py` as the file. Renaming the function would break callers; making `runner.py` a one-line re-export preserves all existing imports (`from app.tools.runner import run_with_retry` keeps working). Cost: a 1-line shim file.

**`code_exec` "reformulate" fallback:** the README says Decomposition is asked to re-plan without code. For E2, do NOT re-invoke Decomposition mid-pipeline — store the request as a flag (`code_exec_failed.suggested_replan=True`) and let Decomposition pick it up on its NEXT invocation (which will be in a subsequent query or after E8's meta-agent loop). Document this as a known scope limitation. Live mid-pipeline re-execution would require a major pipeline restructure and risks infinite loops.

**`retry_reason` vocabulary (closed set):** `"timeout"`, `"empty_result"`, `"malformed"`, `"exec_error"`, `"not_accepted"`, `"malformed_schema_hint_injected"`. Anything else is a bug.

**Fallback idempotency:** track via `ctx.agent_outputs["__fallbacks_fired__"]: set[tuple[str,str]]`. Cheap; per-job; cleared with the SharedContext.

## Verification

**Commands:**
- `docker compose exec api pytest tests/test_retry_fsm.py tests/test_fallbacks.py tests/test_tool_sql_lookup.py -v` — expected: all green.
- `docker compose exec db psql -U mega -d mega -c "\d tool_calls"` — expected: includes `retry_reason text`.
- Smoke: force a Tavily timeout (set `TAVILY_API_KEY=tv-bogus`, point `httpx` at an unreachable host via `/etc/hosts` or monkeypatch in a test) → SSE stream contains exactly one `WEB_FALLBACK` event between three failed `tool_call_end` events.

## Suggested Review Order

**Retry FSM (entry point)**

- The state machine — 2 retries by default, retry_reason derived from prior, hint-aware vocabulary.
  [`retry.py:85`](../../backend/app/tools/retry.py#L85)

- Tool-fn exception capture — turns crashes into synthetic EXEC_ERROR results without breaking the loop.
  [`retry.py:128`](../../backend/app/tools/retry.py#L128)

- Infra-error safety nets — publish_event / DB INSERT degraded paths.
  [`retry.py:57`](../../backend/app/tools/retry.py#L57)

- sql_lookup hint injection + audit-honest retry_reason on attempt 2.
  [`retry.py:170`](../../backend/app/tools/retry.py#L170)

- Back-compat shim so existing `from app.tools.runner import run_with_retry` keeps working.
  [`runner.py:1`](../../backend/app/tools/runner.py#L1)

**Fallback registry**

- The four registered fallbacks, keyed by `(tool_name, error_code)`.
  [`fallbacks.py:104`](../../backend/app/tools/fallbacks.py#L104)

- web_search TIMEOUT re-uses self_reflection via the same retry path (max_retries=0).
  [`fallbacks.py:30`](../../backend/app/tools/fallbacks.py#L30)

- maybe_dispatch — swallows fallback exceptions → `FALLBACK_FAILURE` event.
  [`fallbacks.py:112`](../../backend/app/tools/fallbacks.py#L112)

**Tool-side hint consumer**

- sql_lookup branches on `__retry_hint__` for compact-schema prompt.
  [`sql_lookup.py:190`](../../backend/app/tools/sql_lookup.py#L190)

- Compact-schema loader (table names only).
  [`sql_lookup.py:85`](../../backend/app/tools/sql_lookup.py#L85)

**Agent integration**

- Synthesis prepends a separate `WEB_UNAVAILABLE` segment so output_hash matches the stream.
  [`synthesis.py:124`](../../backend/app/agents/synthesis.py#L124)

- Decomposition prefixes its user prompt with a code-exec avoidance note when the prior fallback set the flag.
  [`decomposition.py:55`](../../backend/app/agents/decomposition.py#L55)

- Pipeline now passes the real `planned.input` through `run_with_retry` for forensic-quality `tool_calls.input` rows.
  [`pipeline.py:138`](../../backend/app/pipeline.py#L138)

- self_reflection is invoked with `max_retries=0` from the pipeline tail.
  [`pipeline.py:92`](../../backend/app/pipeline.py#L92)

**Schema**

- Idempotent migration adding `retry_reason` to `tool_calls`.
  [`004_tool_calls_retry_reason.sql:1`](../../backend/app/sql/004_tool_calls_retry_reason.sql#L1)

- Bootstrap applies migration after grants.
  [`bootstrap.py:36`](../../backend/app/bootstrap.py#L36)

**Tests**

- FSM tests — 8 scenarios including idempotency.
  [`test_retry_fsm.py:1`](../../backend/tests/test_retry_fsm.py#L1)

- Each fallback callable's side effects in isolation.
  [`test_fallbacks.py:1`](../../backend/tests/test_fallbacks.py#L1)

- Compact-schema hint exercise.
  [`test_tool_sql_lookup.py:1`](../../backend/tests/test_tool_sql_lookup.py#L1)

- ScriptedLLM helper for multi-call sequences.
  [`conftest.py:1`](../../backend/tests/conftest.py#L1)
