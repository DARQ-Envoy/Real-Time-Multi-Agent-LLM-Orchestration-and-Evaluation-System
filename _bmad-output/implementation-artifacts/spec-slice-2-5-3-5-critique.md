---
title: 'Slice 2.5-3.5: Critique Agent + self_reflection tool + 1-retry runner'
type: 'feature'
created: '2026-05-10'
status: 'in-review'
baseline_commit: 'NO_COMMITS'
context:
  - '{project-root}/_bmad-output/planning-artifacts/plan.md'
  - '{project-root}/README.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-slice-1-2-5-real-agents.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Real agents now stream cited answers, but nothing flags claims that aren't supported by the retrieved evidence. The eval harness needs a `critique_agreement` signal, and the run trace needs a real tool invocation through a real (1-retry) policy.

**Approach:** Add a `CritiqueAgent` that runs after every sub-agent in the pipeline, emitting a `CritiqueReport` via Anthropic tool use. Wire the `self_reflection` tool through a `run_with_retry` runner that persists `tool_calls` rows and emits `tool_call_start`/`tool_call_end` SSE events. If Critique on Synthesis flags any span with confidence < 0.4, re-run Synthesis exactly once with the critique appended.

## Boundaries & Constraints

**Always:**
- `CritiqueAgent` calls one tool — `emit_critique_report` with input schema `{reviews: [{span_text: str, confidence_score: float, verdict: "SUPPORTED"|"UNSUPPORTED"|"UNCERTAIN", reason: str}]}`. No prose output, no `token` events.
- Critique runs after **every sub-agent** in the dispatch sequence (decomposition, rag, synthesis). It does NOT run after the orchestrator. It does NOT run after itself.
- `agent_id` for Critique log rows is `critique:<target>` (e.g. `critique:rag`). Per-target rows are distinct.
- `self_reflection` tool signature: `async def run(ctx: SharedContext, llm: LLMClient) -> ToolResult`. Reads `ctx.rag_answer` and `ctx.final_answer`. Uses `llm.call_tool` with tool name `emit_contradictions` returning `{contradictions: [{turn_a, turn_b, description}]}`.
- `run_with_retry(tool_fn, ctx, llm, db_pool, redis, tool_name)` retries at most **once** (max_retries=1). Each attempt writes a `tool_calls` row with `retry_number` 0 or 1. `accepted_by_agent = result.success and result.data is not None and result.data not in ([], {})`. Each attempt emits `tool_call_start` + `tool_call_end` SSE events.
- Pipeline contradiction-resolution: if any `ClaimReview` in the synthesis-critique has `confidence_score < 0.4`, re-run `SynthesisAgent` once with the critique reports appended to its user message. The second `AGENT_END` row for synthesis carries `policy_violations="RESOLUTION_LOOP_RUN"` (concatenated to any agent-self-reported violation). Cap at one loop.
- SSE wire format unchanged from Slice 1-2.5. No new event types are introduced — only `tool_call_start` and `tool_call_end` are now actually emitted (their models already existed).
- The pipeline's existing `for agent_id in sequence` dispatch loop is **not** refactored. Critique invocations are added inside it; tool + resolution loop are added after it.

**Ask First:**
- Adding additional tools (`web_search`, `code_exec`, `sql_lookup`) — out of scope for this slice.
- Increasing retry count beyond 1 (README says 2; this slice says 1).
- Re-running Critique after the resolution-loop second Synthesis pass.

**Never:**
- No second-order critique loops. Critique on Critique is forbidden.
- No streaming token events from the Critique Agent.
- No automatic Synthesis re-runs beyond the single resolution-loop pass.
- **Cut policy:** if elapsed slice time exceeds hour 3.5 (`CUT_RESOLUTION_LOOP=true`), skip the resolution loop entirely. Critique reports still attach to `SharedContext` and persist to `agent_logs`.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Real query, all clear | Synthesis output well-supported | Each sub-agent followed by critique:&lt;target&gt; AGENT_START/END; self_reflection tool emits tool_call_start/end; no resolution loop; jobs.status=COMPLETE | n/a |
| Synthesis low-confidence span | Synthesis critique has any review with confidence_score &lt; 0.4 | Synthesis runs a 2nd time with critique appended; 2nd AGENT_END policy_violations contains "RESOLUTION_LOOP_RUN" | one-pass cap |
| Cut policy on | `CUT_RESOLUTION_LOOP=true` | No 2nd Synthesis run even if low-confidence spans found; critique reports still persisted | n/a |
| Critique tool returns empty/malformed | LLM returns no tool_use or input schema fails Pydantic | Empty `CritiqueReport(reviews=[])` stored; AGENT_END policy_violations="CRITIQUE_EMPTY"; pipeline continues | wrap in try/except |
| self_reflection on empty context | Both `rag_answer` and `final_answer` empty | ToolResult(success=False, error_code="EMPTY", data=None); accepted=false; tool_calls row persisted with retry_number=0; one retry attempted | tool returns EMPTY |
| self_reflection LLM error | Anthropic call raises | ToolResult(success=False, error_code="EXEC_ERROR"); 1 retry; if both fail, run continues (advisory tool, non-blocking) | try/except in tool |
| Synthesis 2nd run also flagged | Resolution loop ran but critique would still flag low-confidence | NOT re-critiqued; first synthesis output is kept; 2nd output replaces final_answer | one-pass cap |

</frozen-after-approval>

## Code Map

- `backend/app/models.py` -- add `ClaimReview`, `CritiqueReport`, `ContradictionSpan`; extend `SharedContext` with `critique_reports: list[CritiqueReport]` and `resolution_loop_active: bool=False`
- `backend/app/agents/prompts/critique.md` -- terse system prompt (per locked decision #10)
- `backend/app/agents/critique.py` -- `CritiqueAgent(llm, target_agent_id)`. Class `agent_id` is set in `__init__` to `critique:<target>`. Reads the target's output from `ctx`, calls `emit_critique_report` tool, appends report to `ctx.critique_reports`. Yields only AGENT_START + AGENT_END.
- `backend/app/tools/__init__.py` -- empty marker
- `backend/app/tools/self_reflection.py` -- `async def run(ctx, llm) -> ToolResult` using `emit_contradictions` tool
- `backend/app/tools/runner.py` -- `run_with_retry(tool_fn, ctx, llm, db_pool, redis, tool_name)`; persists `tool_calls` rows; emits SSE events; max 1 retry
- `backend/app/pipeline.py` -- inside the existing for-loop, append CritiqueAgent invocation after each sub-agent. After the loop: invoke self_reflection via runner; check synthesis critique → resolution loop (gated by `CUT_RESOLUTION_LOOP`).
- `backend/app/settings.py` -- add `CUT_RESOLUTION_LOOP: bool = False`
- `backend/app/agents/synthesis.py` -- when `ctx.resolution_loop_active`, append the most recent `synthesis` critique report to the user message; otherwise unchanged.
- `backend/app/agents/base.py` -- no change needed; `agent_id` becomes instance-settable (already attribute, not class-locked at use sites).

## Tasks & Acceptance

**Execution:**
- [x] `backend/app/models.py` -- add ClaimReview, CritiqueReport (`{target_agent_id, reviews}`), ContradictionSpan; extend SharedContext
- [x] `backend/app/settings.py` -- CUT_RESOLUTION_LOOP flag
- [x] `backend/app/agents/prompts/critique.md` -- terse prompt
- [x] `backend/app/agents/critique.py` -- tool-use only, no token events; per-target agent_id
- [x] `backend/app/tools/__init__.py` + `self_reflection.py` + `runner.py` -- self_reflection tool, retry runner, tool_calls persistence, SSE events
- [x] `backend/app/agents/synthesis.py` -- read critique feedback when resolution_loop_active
- [x] `backend/app/pipeline.py` -- critique after each sub-agent; self_reflection invocation; resolution-loop one-pass; cut-policy gate; pass extra_violation through `_run_agent`

**Acceptance Criteria:**
- Given a real LLM key and a query like "What does the README say about retry policy?", when the run completes, then `agent_logs WHERE agent_id LIKE 'critique:%'` has at least one row per executed sub-agent (decomposition if present, rag, synthesis).
- Given the same run, when querying `tool_calls WHERE job_id=$1 AND tool_name='self_reflection'`, then ≥1 row exists with `retry_number IN (0, 1)` and a non-null `latency_ms`.
- Given the run, when watching SSE, then the stream contains at least one `tool_call_start` and one matching `tool_call_end` for `self_reflection`.
- Given a deliberately ambiguous query that would produce non-`SUPPORTED` verdicts (e.g., "Explain the impact"), when the run completes, then at least one row in `agent_logs WHERE agent_id LIKE 'critique:%'` has a non-null `output_hash` and the corresponding `ctx.critique_reports` contains ≥1 review with `verdict != "SUPPORTED"`. Verify via the synthesis-critique row's report content (printed at end-of-run for inspection).
- Given a synthesis-critique with any `confidence_score < 0.4` and `CUT_RESOLUTION_LOOP=false`, when the pipeline finishes, then there are exactly two `agent_logs` rows with `agent_id='synthesis'` of `event_type='AGENT_END'`, and the second's `policy_violations` contains "RESOLUTION_LOOP_RUN".
- Given `CUT_RESOLUTION_LOOP=true`, when the same query runs, then there is exactly one `synthesis` AGENT_END row regardless of critique scores.

## Spec Change Log

- **2026-05-10 — Critique summary stdout dump:** added `_dump_critique_summary()` in `pipeline.py` that prints per-target verdict counts (`S=… U=… Q=…`) to worker stdout at end of each run. The full `CritiqueReport` is not persisted to a queryable column (per-spec constraint), so this print is the only externally-visible artifact for AC4. Verified AC4 via `docker logs mega-worker | grep critique-summary`.
- **2026-05-10 — Self-reflection accept-rule observation:** the model frequently returned `contradictions=[]` (a successful but empty result), which the conservative accept-rule (`success AND data not in (None,[],{})`) treats as not-accepted, triggering the one allowed retry. Both attempts produced empty arrays for typical queries → 2 `tool_calls` rows per run with `accepted=false`. This is the designed behaviour. KEEP.
- **2026-05-10 — AC5/AC6 verification:** to exercise the resolution-loop mechanism (the model produces confidence=1.0 for SUPPORTED reviews, so the `< 0.4` threshold never fires naturally), `LOW_CONFIDENCE_THRESHOLD` was temporarily raised to 2.0 in pipeline.py, and `CUT_RESOLUTION_LOOP=true` was temporarily set in compose. Both verified live (job `00c81bca…` shows two synthesis AGENT_END rows with the second tagged `RESOLUTION_LOOP_RUN`; job `5ca08a54…` shows only one synthesis row under cut). Threshold and compose env both reverted.

## Design Notes

**Why `span_text` instead of character offsets:** Character offsets break under whitespace/punctuation drift between what the LLM sees and what we store. Verifying that `span_text` is a substring of the target output is one line; verifying offsets correctly is a class of bugs.

**Why critique skips the orchestrator:** The orchestrator output is a structural `RoutingPlan`, not a factual claim. Critiquing it would require a completely different rubric and adds no eval signal in this slice.

**Resolution-loop input shape:** Synthesis, when `ctx.resolution_loop_active=True`, reads the most recent `target_agent_id="synthesis"` critique report from `ctx.critique_reports` and appends a section like:

```
Prior critique flagged these spans:
- "<span_text>" (verdict=UNSUPPORTED, confidence=0.32): <reason>
Revise the answer to address each.
```

The second pass produces a new `final_answer` list, which overwrites the first. The first answer is recoverable only via the `agent_logs` row `output_hash` of the first synthesis run (out of scope to expose).

**`run_with_retry` accept rule:** Empty data (`[]`, `{}`, `None`) counts as not-accepted, triggering retry. This is more conservative than just checking `success=True`, which would accept "tool ran but returned nothing".

## Verification

**Commands:**
- `docker compose build api worker && docker compose up -d --force-recreate api worker` -- expected: api+worker rebuild and start
- `curl -X POST http://localhost:8000/query -H 'content-type: application/json' -d '{"query":"What does the README say about retry policy?"}'` -- expected: 202 with job_id
- `curl -N http://localhost:8000/stream/<job_id>` -- expected: SSE stream containing `agent_start` for `critique:rag`, `critique:synthesis`, plus `tool_call_start`/`tool_call_end` for `self_reflection`
- `docker compose exec db psql -U mega -d mega -At -c "SELECT agent_id, event_type, policy_violations FROM agent_logs WHERE job_id=(SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1) ORDER BY id;"` -- expected: rows for orchestrator, [decomposition], rag, critique:rag, [critique:decomposition], synthesis, critique:synthesis (and possibly second synthesis with RESOLUTION_LOOP_RUN)
- `docker compose exec db psql -U mega -d mega -At -c "SELECT tool_name, retry_number, success, accepted FROM tool_calls WHERE job_id=(SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1);"` -- expected: ≥1 row for self_reflection
