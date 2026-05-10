---
title: 'Slice 1-2.5: Real Orchestrator + Decomp + RAG + Synthesis with Anthropic streaming'
type: 'feature'
created: '2026-05-10'
status: 'in-review'
baseline_commit: 'NO_COMMITS'
context:
  - '{project-root}/_bmad-output/planning-artifacts/plan.md'
  - '{project-root}/README.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-slice-0-1-vertical-spine.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The spine streams hardcoded tokens. Real reasoning, retrieval, and citations are needed before the eval harness has anything meaningful to score.

**Approach:** Replace `StubAgent` with a 4-agent pipeline (Orchestrator → Decomposition → RAG → Synthesis) backed by Anthropic streaming `messages.stream()`. RAG retrieves over a hardcoded 8-chunk README corpus with a 2-hop reformulation loop. Synthesis emits a `list[SentenceProvenance]` with chunk-level citations. Every agent's I/O is hashed and persisted to `agent_logs`.

## Boundaries & Constraints

**Always:**
- Use `anthropic>=0.40` SDK exclusively. All LLM calls go through `messages.stream()`. Text deltas → SSE `token` events as they arrive.
- Orchestrator emits `RoutingPlan` via Anthropic tool use (single tool `emit_routing_plan` with `tool_choice` forced). Required input fields: `agent_sequence`, `justification`. Other RoutingPlan fields stay default.
- RAG enforces exactly two hops: keyword retrieval → LLM-driven query reformulation → second retrieval. Top-3 per hop. Final RAG output cites all retrieved chunk_ids it actually used.
- `CORPUS` = exactly 8 hardcoded chunks from `README.md` covering: retry policy, agent boundaries, schema, eval pipeline, streaming, self-improvement, tool catalogue, context budget. Each `{chunk_id, text, source_url}`.
- Routing/dispatch loop lives in `app/pipeline.py`. `worker.py` calls `pipeline.run(shared_ctx, redis, db_pool)`; it does not instantiate agents.
- Prompts in `app/agents/prompts/{orchestrator,decomp,rag,synthesis}.md` — plain markdown, loaded at agent-class construction time.
- `final_answer` becomes `list[SentenceProvenance]` (`{sentence_text, source_agent, source_chunk_ids, contradiction_resolved=False}`).
- Each agent run produces an `AGENT_START` and `AGENT_END` row in `agent_logs` via `log_agent_event(conn, job_id, agent_id, event_type, input_hash, output_hash, latency_ms)`.
- SSE wire format from Slice 0-1 stays unchanged. New event types are not introduced in this slice.

**Ask First:**
- Adding Critique Agent, contradiction loop, or any tool from the catalogue (`web_search`, `code_exec`, `sql_lookup`, `self_reflection`).
- Changing the `agent_logs` columns or `SSEEvent` discriminator values.

**Never:**
- No FAISS / pgvector / embeddings — keyword search only.
- No real `web_search` or `code_exec` tool calls. Only the `emit_routing_plan` tool exists in this slice.
- No parallel agent execution. Sequential dispatch only.
- No streaming-token emission for the Orchestrator's tool-use payload (tool_use input deltas are JSON, not user-facing text).
- **Cut policy:** if elapsed slice time crosses 90 minutes before Synthesis is wired, skip Decomposition entirely. Orchestrator routes Query → RAG → Synthesis. Document the cut in the Spec Change Log.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Real query happy path | POST /query "What does the README say about retry policy?" | SSE stream emits ≥10 events; `final_answer` is non-empty `SentenceProvenance` list with ≥2 distinct `source_chunk_ids` | n/a |
| LLM API key missing | `LLM_API_KEY` empty/stub at job start | Emit `error` SSE event `LLM_NOT_CONFIGURED`, set jobs.status=FAILED, still emit `job_complete` so client unblocks | catch in pipeline.run, log to agent_logs |
| Anthropic transient error | LLM call raises | Bubble up to pipeline, emit `error` event, FAILED status, `job_complete` emitted | try/except per agent |
| RAG hop 1 empty | Keyword search returns 0 chunks | Hop 2 runs anyway with reformulated query; if both empty, RAG outputs `LOW_COVERAGE` flag and Synthesis omits citations | handle in RAG; do not crash |
| Reformulation parse failure | LLM returns non-JSON for hop-2 query | Fall back to original query for hop 2 | wrap in try/except, log policy_violations="REFORMULATION_PARSE_FAIL" |
| Decomposition cut at 90 min | Elapsed > 90 min before Decomp wired | Skip Decomp class entirely; pipeline runs Orch → RAG → Synthesis | applied in pipeline; logged in Spec Change Log |
| Tool-use input malformed | Orchestrator returns RoutingPlan missing required fields | Default to `agent_sequence=["rag","synthesis"]` (or `["rag","synthesis"]` post-cut), justification="default fallback (orchestrator output invalid)" | validate via Pydantic; fall back |

</frozen-after-approval>

## Code Map

- `backend/requirements.txt` -- add `anthropic>=0.40,<1`
- `backend/app/llm.py` -- thin Anthropic client wrapper; `stream_text(prompt, system)` async-yields text deltas; `stream_tool_use(tool_schema, prompt, system)` async-yields text deltas + final tool input dict
- `backend/app/corpus.py` -- `CORPUS: list[Chunk]` (8 entries), `keyword_search(query, k=3) -> list[Chunk]`
- `backend/app/agents/prompts/orchestrator.md` -- system prompt instructing tool-only output
- `backend/app/agents/prompts/decomp.md` -- system prompt for ≥2 typed sub-tasks (this slice: `task_type ∈ {FACTUAL, ANALYTICAL}` only)
- `backend/app/agents/prompts/rag.md` -- system prompt for hop-2 query reformulation and final answer drafting
- `backend/app/agents/prompts/synthesis.md` -- system prompt for sentence-level provenance assembly
- `backend/app/agents/orchestrator.py` -- `OrchestratorAgent` emits `RoutingPlan` via `emit_routing_plan` tool use
- `backend/app/agents/decomposition.py` -- `DecompositionAgent` produces `list[SubTask]` (≥2)
- `backend/app/agents/rag.py` -- `RAGAgent` runs the 2-hop loop, builds an `AnswerSegment` list with `citations`
- `backend/app/agents/synthesis.py` -- `SynthesisAgent` reads SharedContext, streams a final answer, parses into sentences, attributes to `source_agent` + `source_chunk_ids`
- `backend/app/pipeline.py` -- `run(shared_ctx, redis, db_pool)`: instantiate agents, dispatch in order, persist agent_logs, publish all SSE events, build `final_answer`, return `list[SentenceProvenance]`
- `backend/app/persistence.py` -- `log_agent_event(conn, job_id, agent_id, event_type, input_hash, output_hash, latency_ms)` plus `sha256_json(obj)` helper
- `backend/app/models.py` -- add `Chunk`, `SubTask`, `DecompositionResult`, `SentenceProvenance`, `LowCoverageFlag`; extend `SharedContext` with `routing_plan`, `decomposition`, `rag_output`, `final_answer` fields
- `backend/app/worker.py` -- `run_query` now calls `pipeline.run()` and persists the returned `final_answer` to `jobs.final_answer`

## Tasks & Acceptance

**Execution:**
- [x] `backend/requirements.txt` -- append `anthropic>=0.40,<1`
- [x] `backend/app/corpus.py` -- 8 README-derived chunks + keyword search
- [x] `backend/app/llm.py` -- wrap `AsyncAnthropic`; `stream_text` + `stream_tool_use`; raises `LLMNotConfigured` if key missing/stub
- [x] `backend/app/persistence.py` -- log_agent_event + sha256_json helpers
- [x] `backend/app/models.py` -- new types; `SharedContext` fields; `RoutingPlan` defaults `agent_sequence=[]`, `justification=""`
- [x] `backend/app/agents/prompts/{orchestrator,decomp,rag,synthesis}.md` -- 4 prompt files
- [x] `backend/app/agents/orchestrator.py` -- emit RoutingPlan via tool use; AGENT_START/END logs; agent_id="orchestrator"
- [x] `backend/app/agents/decomposition.py` -- ≥2 SubTasks; agent_id="decomposition"
- [x] `backend/app/agents/rag.py` -- 2 hops; emits `token` events while drafting answer; agent_id="rag"
- [x] `backend/app/agents/synthesis.py` -- streams final answer; parses sentences; builds SentenceProvenance list; agent_id="synthesis"
- [x] `backend/app/pipeline.py` -- dispatch loop, cut-policy gate, agent_logs persistence, error → SSE error event
- [x] `backend/app/worker.py` -- swap StubAgent path for `pipeline.run()`; persist returned final_answer

**Acceptance Criteria:**
- Given a real `LLM_API_KEY` is set, when POST /query "What does the README say about retry policy?", then SSE stream emits ≥10 events including `agent_start`/`agent_end` for orchestrator + rag + synthesis (and decomposition unless cut), and a final `job_complete`.
- Given the same query, when checking `jobs.final_answer`, then it is a JSONB array of ≥1 `SentenceProvenance` object whose union of `source_chunk_ids` contains ≥2 distinct chunk_ids.
- Given the run completes, when querying `agent_logs WHERE job_id=$1`, then there are ≥2 rows per agent in the executed `agent_sequence` (one AGENT_START, one AGENT_END), each with non-null `input_hash` and `output_hash`.
- Given `LLM_API_KEY` is the stub default, when POST /query, then SSE emits an `error` event with `error_code="LLM_NOT_CONFIGURED"` followed by `job_complete`, and `jobs.status="FAILED"`.
- Given the cut policy triggered (>90 min mark), when inspecting the run, then `routing_plan.agent_sequence` lacks "decomposition" and the Spec Change Log has a CUT entry.

## Spec Change Log

- **2026-05-10 — Postgres image:** docker-compose uses `postgres:17-alpine` instead of `postgres:16`. Reason: 16-alpine pull was failing in this environment (network drop at ~74MB); 17-alpine was already cached locally and is schema-compatible for our DDL. README still references 16 — Slice 5.5–6 polish should reconcile.
- **2026-05-10 — `LLM_API_KEY` injection:** docker-compose passes `${ANTHROPIC_API_KEY}` from the host into containers as `LLM_API_KEY` so the secret is not written to `.env`. Default falls back to the stub literal so the failure path AC stays reproducible.
- **2026-05-10 — Reformulation observation:** On narrow single-topic queries (e.g. "retry policy"), the LLM-driven hop-2 query reformulation occasionally returns empty/unparseable text, so RAG falls back to the original query and emits `policy_violations="REFORMULATION_PARSE_FAIL"`. This is the designed defensive path — confirmed working end-to-end. KEEP: the fallback path (do not turn this into a fatal error in later slices).
- **2026-05-10 — Cut policy not exercised:** `CUT_DECOMPOSITION` env flag is wired (`settings.py` + `pipeline.py`) but did not need to be flipped — implementation finished comfortably under 90 min. Mechanism untested at runtime; structurally verified (one-line filter in `pipeline.run`).

## Design Notes

**Sentence attribution heuristic (Synthesis):** The Synthesis prompt instructs the LLM to emit sentences in a structured form `[chunk_id_a,chunk_id_b] sentence text.` The agent parses each line back into a `SentenceProvenance` with `source_agent="synthesis"`, `source_chunk_ids=[…]` from the bracketed prefix. Lines without brackets become `source_chunk_ids=[]`. Cheap, no second LLM call.

**Tool-use streaming:** For Orchestrator, we still consume the stream so the `messages.stream()` lifecycle drives state, but we do **not** emit per-delta `token` SSE events from the tool-use input JSON. We emit `agent_start` → (silent stream consumption) → `agent_end`. Token events are reserved for user-visible prose (RAG, Synthesis).

**Keyword search scoring:**
```python
def score(query, chunk_text):
    q = set(re.findall(r"\w+", query.lower()))
    c = set(re.findall(r"\w+", chunk_text.lower()))
    return len(q & c) / max(len(q), 1)
```
Top-3 by descending score, ties broken by `chunk_id`.

## Verification

**Commands:**
- `docker compose build api worker && docker compose up -d` -- expected: 4 containers Up
- `curl -X POST http://localhost:8000/query -H 'content-type: application/json' -d '{"query":"What does the README say about retry policy?"}'` -- expected: 202 with job_id
- `curl -N http://localhost:8000/stream/<job_id>` -- expected: ≥10 SSE data lines including 3-4 distinct agent_start events
- `docker compose exec db psql -U mega -d mega -c "SELECT jsonb_array_length(final_answer) FROM jobs ORDER BY created_at DESC LIMIT 1;"` -- expected: ≥1
- `docker compose exec db psql -U mega -d mega -c "SELECT count(DISTINCT agent_id) FROM agent_logs WHERE job_id=(SELECT id FROM jobs ORDER BY created_at DESC LIMIT 1);"` -- expected: ≥3 (orchestrator, rag, synthesis at minimum)
