# Extended Plan — Full README Coverage

**Project:** Real-Time Multi-Agent LLM Orchestration & Evaluation System
**Spec source:** `README.md` (treated as PRD + Architecture, **every section to the letter**)
**Owner:** Darq · **Sequencing:** John (PM)
**Started:** 2026-05-10

---

## How this document relates to `plan.md`

`plan.md` is the **6-hour MVP** — the slices needed to demo a working system inside the assessment window.
`extended.md` is the **complete spec build-out** — MVP slices verbatim, plus post-MVP slices (`E1–E11`) that fill in everything `plan.md` deliberately cuts.

| Phase | Slices | Time-boxed? | Goal |
|---|---|---|---|
| **Part A — MVP** | `0–1` → `5.5–6` | Yes (6 h total) | Demoable end-to-end system; the spine |
| **Part B — Extended** | `E1` → `E11` | No (post-assessment) | Full README coverage; production-grade depth |
| **Post-extended** | future | No | Items README §What We Would Build Next defers entirely |

Each extended slice **builds on prior work**, replacing fakes with real implementations, adding spec-required surfaces, and increasing complexity in a defensible order.

---

## Guiding principle

The smallest unit isn't *below* the backend — it's *across* it. A vertical slice that streams one token end-to-end through `FastAPI → worker → SharedContext → SSE → client` is the spine. Everything else is reps on the spine.

Cut deliberately, not by hour-5 panic. Re-enable cuts in extended slices in dependency order.

---

## Glossary — what "fake / stub / mock" means here

In MVP slice 0–1 we wire the **runtime** before we make it intelligent. Real wiring, fake brains.

| Term | Meaning | Example | Replaced in |
|---|---|---|---|
| **Stubbed agent** | A real Python class implementing the agent interface, but its `run()` returns hardcoded output instead of calling an LLM. | `StubAgent.run()` yields `["Routing query.", "Looking up.", "Done."]` with 200 ms sleep between each. | Slice `1–2.5` |
| **Mock tokens** | SSE `token` events emitted from a hardcoded list, not real LLM streaming. Same event shape. | `yield SSEEvent(type="token", agent_id="stub", text=word)` | Slice `1–2.5` (real LLM), slice `E5` (per-token granularity) |
| **Fake corpus** | 5–10 hardcoded text chunks; RAG retrieves by keyword match, not vectors. | `CORPUS = [{"chunk_id": "c1", "text": "...", "source_url": "..."}]` | Slice `E4` (real FAISS) |
| **1-retry tool policy** | Tool retries once on failure, then falls back. | `with_retry(tool, max_retries=1)` | Slice `E2` (full 2-retry FSM) |
| **Sequential agent execution** | Orchestrator runs sub-agents in fixed order; no dependency graph. | `await run(decomp); await run(rag); await run(critique)` | Slice `E5`-adjacent (full `RoutingPlan` with `dependency_edges`) |

Track each replacement in commit messages: `slice E4: replace fake corpus with FAISS index`.

---

## Agent runbook

| Agent | Command (verbatim) | What you'll get back |
|---|---|---|
| **John** (PM) | `bmad-agent-pm <question>` | Scope decisions, cut calls, sequencing. Returns when scope drifts. |
| **Winston** (Architect) | `bmad-agent-architect <prompt>` | Design docs, ADRs, file structure choices. **Does not write production code.** |
| **Amelia** (Dev) | `bmad-agent-dev <prompt>` | Code execution against a story file. Use when you've created a story via `bmad-create-story`. |
| **Quick Dev** (no persona) | `bmad-quick-dev <prompt>` | Direct intent-to-code. Skips story ceremony. **Fastest path, recommended for MVP.** |

Always start each agent in a **fresh context window**. Paste the prompt right after the command name.

For extended slices (`E1–E11`), recommended pattern: `bmad-create-story` from each slice description, then `bmad-agent-dev` to execute. Story ceremony is worth it once the spine exists and you're working in a system that resists ad-hoc edits.

---

## How to complete Slice 0–1 (concrete steps)

**Recommended path (1 invocation, ~45 min runtime):**

```
bmad-quick-dev Implement Slice 0–1 of the assessment per _bmad-output/planning-artifacts/plan.md.
Goal: vertical spine of FastAPI + Postgres + ARQ + Redis + 1 stubbed agent producing SSE.
Read plan.md fully before starting. Stop at the acceptance criteria — do not proceed to Slice 1–2.5.
```

**If you want an architecture sanity check first (adds ~15 min):**

```
bmad-agent-architect Winston, review Slice 0–1 of _bmad-output/planning-artifacts/plan.md.
I want a 1-page sanity check on file structure, asyncpg vs SQLAlchemy, ARQ task signature,
and SSE-via-Redis-pub-sub vs in-process queue. No full ADR. Flag risks only.
```

Then run the `bmad-quick-dev` invocation above with `+ Winston's notes attached`.

**Skip:** the BMad orthodox flow (`bmad-create-story` → `bmad-agent-dev`). Story ceremony costs 30–45 min for a slice this small. Save Amelia/stories for slice `1–2.5` onward.

---

# Part A — MVP slices (Hour 0 → 6)

## Slice 0–1 — Vertical spine (Hour 0 → 1)

**Outcome:** Prove the wire works end-to-end. POST → queue → worker → SSE → client. No real LLM yet.

### Stack decisions (locked)

- Python 3.12 · FastAPI · Uvicorn (ASGI)
- `pydantic-settings` for env config · Pydantic v2 for models
- `asyncpg` (no ORM — schema is small and stable)
- ARQ for background jobs · Redis 7 as broker + SSE pub/sub bus
- Postgres 16
- SSE via FastAPI `StreamingResponse` subscribing to a Redis channel `job:<job_id>`

### File structure to create

```
backend/
  app/
    __init__.py
    main.py            # FastAPI app: GET /healthz, POST /query, GET /stream/{job_id}
    settings.py        # pydantic-settings: DATABASE_URL, REDIS_URL, LLM_API_KEY, MAX_BUDGET_TOKENS
    db.py              # asyncpg pool lifecycle (startup/shutdown)
    redis_bus.py       # publish_event(job_id, event) / subscribe(job_id) helpers
    models.py          # SharedContext, RoutingPlan, ToolResult, AnswerSegment,
                       #   JobRequest, JobResponse, SSEEvent (discriminated union)
    agents/
      __init__.py
      base.py          # AgentBase ABC: async run(ctx) -> AsyncIterator[SSEEvent]
      stub.py          # StubAgent — yields 3 hardcoded token events
    worker.py          # ARQ WorkerSettings + run_query(ctx, job_id, query) task
    sql/
      001_init.sql     # Schema verbatim from README §Database Schema
    bootstrap.py       # Runs 001_init.sql on startup if tables missing
  Dockerfile           # Python 3.12-slim, non-root, multi-stage
  pyproject.toml       # OR requirements.txt — fastapi, uvicorn, asyncpg, arq,
                       #   redis, pydantic, pydantic-settings, python-dotenv
docker-compose.yml     # services: api, worker, db (postgres:16), redis (redis:7-alpine)
.env.example           # documents every env var with safe defaults
```

### Endpoint contracts

| Endpoint | Method | Request | Response |
|---|---|---|---|
| `/healthz` | GET | — | `200 {"status":"ok","db":"ok","redis":"ok"}` |
| `/query` | POST | `{"query":"<str>","max_budget_tokens":16384}` | `202 {"job_id":"<uuid>","stream_url":"/stream/<uuid>"}` |
| `/stream/{job_id}` | GET | — | `text/event-stream` of `SSEEvent`s until `job_complete` |

### What StubAgent must stream (event sequence)

```jsonc
{"type":"agent_start",  "agent_id":"stub", "budget_remaining":4096}
{"type":"token",        "agent_id":"stub", "text":"Routing query."}
{"type":"token",        "agent_id":"stub", "text":"Looking up."}
{"type":"token",        "agent_id":"stub", "text":"Done."}
{"type":"agent_end",    "agent_id":"stub", "output_hash":"<sha256>", "policy_violations":null}
{"type":"job_complete", "job_id":"<uuid>", "total_latency_ms":<float>}
```

The `SSEEvent` discriminated union must be defined in `models.py` so slice `1–2.5`'s real agents drop in without changing the wire format.

### Acceptance criteria (all must pass before moving on)

1. `docker compose up --build` runs cleanly; all four containers stay up for ≥30 s.
2. `curl http://localhost:8000/healthz` → 200 with all three subsystems `"ok"`.
3. `docker compose exec db psql -U mega -d mega -c '\dt'` lists: `jobs, agent_logs, tool_calls, eval_runs, eval_cases, prompt_rewrites, performance_deltas`.
4. `curl -X POST http://localhost:8000/query -H 'content-type: application/json' -d '{"query":"hello"}'` → 202 with `job_id` and `stream_url`.
5. `curl -N http://localhost:8000/stream/<job_id>` emits ≥5 `data: ...` lines (start + 3 tokens + end + complete) within 5 s.
6. After stream finishes, row in `jobs` table has `status = 'COMPLETE'` and `final_answer` is non-null.

### Cut if behind

None. This is the spine. Without it, no later slice runs.

---

## Slice 1–2.5 — Real Orchestrator + Decomp + RAG + Synthesis (Hour 1 → 2.5)

**Outcome:** Real LLM calls, real reasoning, real provenance. Stub agent gone.

- Replace `StubAgent` with `OrchestratorAgent` that emits a `RoutingPlan` (function-calling JSON).
- `DecompositionAgent` produces ≥2 typed sub-tasks for ambiguous queries.
- `RAGAgent` retrieves over a 5–10-chunk hardcoded `CORPUS` (no FAISS yet); enforces 2 hops.
- `SynthesisAgent` merges with a `SentenceProvenance` list.
- Persist all agent I/O to `agent_logs` with input/output hashes.

**Cut if behind:** drop `DecompositionAgent`; Orchestrator routes straight to RAG.

**Acceptance:** A real query like *"What does the README say about retry policy?"* produces a cited answer streamed live, with at least 2 distinct `chunk_id`s in the provenance.

---

## Slice 2.5–3.5 — Critique Agent + self_reflection tool (Hour 2.5 → 3.5)

**Outcome:** Critique annotates Synthesis output with claim-level confidence. One real tool wired through the retry policy.

- `CritiqueAgent` produces `ClaimReview` objects (span + confidence + verdict).
- `self_reflection` tool reads `SharedContext`, returns contradictions list. Cheapest tool — no external API.
- Tool retry: 1 retry, then fallback (not the full 2-retry FSM).
- Contradiction-resolution loop runs at most once.

**Cut if behind:** drop the contradiction loop; just annotate without resolving.

**Acceptance:** Trace shows Critique runs after every sub-agent, with at least one non-`SUPPORTED` verdict on a deliberately ambiguous test query.

---

## Slice 3.5–4.5 — Frontend (Hour 3.5 → 4.5)

**Outcome:** A user can submit a query and watch agents stream live, with a final answer + provenance panel.

- Static HTML + vanilla JS + `EventSource` API (no React/Vite/build step).
- Three panels: query input, live event log (color-coded by `agent_id`), final answer with citations.
- Served by FastAPI from `/` or `/static/index.html`.

**Cut if behind:** drop the live event log; only show the final answer.

**Acceptance:** Open `http://localhost:8000`, submit "What is the retry policy?", see ≥10 events stream in, see a final answer with at least one citation link.

---

## Slice 4.5–5.5 — Eval harness (Hour 4.5 → 5.5)

**Outcome:** 5 hand-written eval cases, scored on 3 dimensions, results in `eval_runs` / `eval_cases` tables.

- Cases: 1 baseline + 2 ambiguous + 2 adversarial (1 prompt-injection + 1 false-premise).
- Dimensions: `answer_correctness`, `citation_accuracy`, `critique_agreement`. Skip the other 3.
- `eval/scorer.py` — pure Python, no LLM-as-judge for `citation_accuracy` (literal chunk_id match).
- One CLI: `python -m eval.run` — produces an `eval_run` row + 5 `eval_cases` rows.

**Cut if behind:** skip running eval; commit the harness skeleton + a `docs/eval-design.md` describing what would be measured.

**Acceptance:** `python -m eval.run` exits 0 and prints a per-dimension summary; `eval_runs` has 1 row, `eval_cases` has 5.

---

## Slice 5.5–6 — Polish (Hour 5.5 → 6)

- README §Quick Start verified by following it on a fresh machine (or fresh `docker compose down -v`).
- README §What We Would Build Next expanded with everything in the cut list below.
- README §AI Collaboration Disclosure filled in honestly.
- Demo recording (Loom or screen capture, ≤3 min): query → stream → answer → eval CLI.
- One clean commit per slice on `main`. No stray branches.

---

## MVP cut list (re-enabled in Part B)

| Cut item | Re-enabled in |
|---|---|
| `web_search`, `code_exec`, `sql_lookup` tools | Slice **E1** |
| Full 2-retry FSM + per-tool fallback contracts | Slice **E2** |
| `ContextBudgetManager` (full) + Compression Agent | Slice **E3** |
| FAISS-backed RAG (replaces hardcoded `CORPUS`) | Slice **E4** |
| Token-level streaming + full SSE event coverage | Slice **E5** |
| `GET /trace/{job_id}` + `GET /evals/latest` + `/evals/diff` | Slice **E6** |
| 15-case eval set + all 6 scoring dimensions | Slice **E7** |
| Meta-Agent + `prompt_rewrites` table population | Slice **E8** |
| `/rewrites/{id}/approve\|reject` + `/eval/rerun` + `performance_deltas` | Slice **E9** |
| Datasette log UI on port 8080 | Slice **E10** |
| Auth (API key) + rate limiting + request ID | Slice **E11** |

---

# Part B — Extended slices (post-MVP, full spec coverage)

Each extended slice **builds on prior work**. Acceptance criteria assume all earlier slices (MVP + earlier extended) are green.

---

## Slice E1 — Full tool layer (`web_search`, `code_exec`, `sql_lookup`)

**Builds on:** Slice `2.5–3.5` (`self_reflection` exists; `tool_calls` table populated).

**Outcome:** All four tools from README §Tool Catalogue wired with documented failure modes.

### Files

```
backend/app/tools/
  __init__.py
  base.py              # ToolBase ABC; ToolResult model (already partly in models.py)
  registry.py          # name → class mapping; Orchestrator looks up tools by name
  web_search.py        # Tavily/Brave/SerpAPI client; 5 s timeout
  code_exec.py         # subprocess sandbox; banned-import allowlist; 10 s timeout; no network
  sql_lookup.py        # NL → SQL via LLM; executes against read-replica role; 8 s timeout
  self_reflection.py   # already shipped in slice 2.5–3.5 — no change
```

### Behavior contracts (verbatim from README §Tool Catalogue)

| Tool | Timeout | Failure → `error_code` | Notes |
|---|---|---|---|
| `web_search` | 5 s | TIMEOUT / EMPTY / MALFORMED | Returns ≤10 results: `title, url, snippet, relevance_score` |
| `code_exec` | 10 s | TIMEOUT / EMPTY / MALFORMED | Subprocess; no network; banned-import list enforced |
| `sql_lookup` | 8 s | TIMEOUT / EMPTY / MALFORMED | NL → SQL; uses read replica; SQL string returned in `error_message` for debugging |
| `self_reflection` | n/a | EMPTY / EXEC_ERROR | Reads `SharedContext`; returns `contradictions: List[ContradictionSpan]` |

### Acceptance criteria

1. Each tool returns the correct `error_code` for each failure mode (5 unit tests per tool).
2. `tool_calls` table records every invocation with `tool_name`, `input`, `output`, `latency_ms`, `success`, `error_code`.
3. `code_exec` rejects scripts containing `import os|subprocess|socket|urllib|requests`.
4. `sql_lookup` only connects to a Postgres role with `SELECT` privileges (separate `mega_ro` role).
5. `web_search` requires `WEB_SEARCH_API_KEY` env var; missing key → tool reports `MALFORMED` (not crashes).

### Defer

- gVisor / Firecracker sandbox replacing subprocess (post-extended #3).

---

## Slice E2 — Full retry FSM + per-tool fallback contracts

**Builds on:** Slice `E1` (all four tools live).

**Outcome:** Two retries per tool with `retry_reason` logged. Per-tool fallback contracts honored exactly as README specifies.

### State machine

```
Tool invocation
       │
   ┌───▼────┐         ┌──────┐         ┌──────┐         ┌────────────┐
   │Attempt │─error──▶│Retry1│─error──▶│Retry2│─error──▶│  Fallback  │
   │  (#0)  │         │ (#1) │         │ (#2) │         │ (per-tool) │
   └────┬───┘         └──┬───┘         └──┬───┘         └─────┬──────┘
        │success         │success         │success            │
        ▼                ▼                ▼                   ▼
   accepted_by_agent? ─no─▶ trigger retry  ─yes─▶ commit ToolResult
```

Each retry creates a new `tool_calls` row with `retry_number` ∈ {1, 2} and a non-null `retry_reason`.

### Per-tool fallback contracts (verbatim from README)

| Tool | Trigger | Fallback action | Side-effect |
|---|---|---|---|
| `web_search` | TIMEOUT after 2 retries | Invoke `self_reflection`; mark answer `WEB_UNAVAILABLE` in `SharedContext` | Log `WEB_FALLBACK` event |
| `code_exec` | EXEC_ERROR after 2 retries | Orchestrator logs `TOOL_FAILURE`; asks `DecompositionAgent` to reformulate the sub-task without code | New `RoutingPlan` written |
| `sql_lookup` | MALFORMED on retry 1 | Retry once with simplified schema hint injected | If still MALFORMED after retry 2, skip + log `SQL_FALLBACK_SKIPPED` |
| `self_reflection` | EXEC_ERROR | Orchestrator logs and continues (advisory, non-blocking) | None |

### Files

```
backend/app/tools/
  retry.py             # @with_retry(tool, max_retries=2) decorator
  fallbacks.py         # FALLBACK_REGISTRY: dict[(tool_name, error_code) → callable]
```

### Acceptance criteria

1. Forced timeout on `web_search` produces 2 `tool_calls` rows (retry 1 + 2) and 1 `self_reflection` invocation row; final answer's metadata includes `WEB_UNAVAILABLE`.
2. `tool_calls.retry_reason` is populated for every row with `retry_number > 0`.
3. `accepted_by_agent` is set to `False` before any retry is requested by an agent.
4. After fallback, no further retries fire on the same tool/job (idempotency check).

---

## Slice E3 — Full `ContextBudgetManager` + Compression Agent

**Builds on:** All MVP slices (every agent is real and writing to `SharedContext`).

**Outcome:** Every agent's context-append is gated by `check_budget()`. Overflows trigger Compression. Sidecar stored in Postgres.

### `ContextBudgetManager` API (README §Context Window Management)

```python
class ContextBudgetManager:
    agent_budgets: Dict[AgentID, int]   # tokens — declared as class constants on each agent
    agent_usage:   Dict[AgentID, int]   # current consumption

    def check_budget(self, agent_id: str, tokens_to_add: int) -> bool: ...
    def consume(self, agent_id: str, tokens: int) -> None: ...           # raises BudgetExceeded
    def report_violation(self, agent_id: str, overflow_tokens: int) -> None: ...
```

### Default budgets (configurable via env)

| Agent | Default tokens | Env var |
|---|---|---|
| Orchestrator | 4,096 | `BUDGET_ORCHESTRATOR` |
| Decomposition | 3,072 | `BUDGET_DECOMP` |
| RAG | 6,144 | `BUDGET_RAG` |
| Critique | 4,096 | `BUDGET_CRITIQUE` |
| Synthesis | 8,192 | `BUDGET_SYNTHESIS` |
| Compression | 2,048 | `BUDGET_COMPRESSION` |

### Compression Agent behavior

- Triggered by Orchestrator on `BUDGET_REQUEST` event from any agent.
- **Lossless** on tool outputs, JSON scores, citation objects → SHA-256 hashed → stored in `compressed_sidecars` table; only a reference token in compressed context.
- **Lossy** on conversational filler: acknowledgements, restatements, internal CoT not containing structured claims.
- Output: `CompressedContext` with `compression_ratio`, `lossless_fields_preserved: List[str]`, `summary` (lossy portion).

### Files

```
backend/app/budget.py                # ContextBudgetManager
backend/app/agents/compression.py    # CompressionAgent
backend/app/sql/002_compression.sql  # compressed_sidecars table
```

```sql
-- 002_compression.sql
CREATE TABLE compressed_sidecars (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES jobs(id),
    field_hash      TEXT NOT NULL,        -- SHA-256 of original content
    field_kind      TEXT NOT NULL,        -- tool_output | citations | scores
    content         JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX idx_sidecars_hash ON compressed_sidecars(field_hash);
```

### Post-execution overflow detection

After each agent finishes, the worker counts tokens in its committed context. If the count exceeds the agent's declared budget, write `policy_violations = 'BUDGET_OVERFLOW:<overflow>'` into the corresponding `agent_logs` row. This catches agents that bypass `check_budget()`.

### Acceptance criteria

1. With `BUDGET_SYNTHESIS=512`, a long-answer query triggers Compression; one `compressed_sidecars` row exists; `compression_ratio < 1.0`; final answer references compressed chunks correctly.
2. A test agent that bypasses `check_budget()` and overflows produces a `policy_violations` entry in `agent_logs`.
3. `budget_compliance` score (slice E7) decreases for jobs with violations.

---

## Slice E4 — Real RAG with FAISS

**Builds on:** Slice `1–2.5` (`RAGAgent` exists with hardcoded `CORPUS`).

**Outcome:** Hardcoded corpus replaced with FAISS index loaded from disk at container start. Retrieval quality bounded only by seeded corpus.

### Files

```
backend/app/rag/
  __init__.py
  embedder.py          # OpenAI text-embedding-3-small OR sentence-transformers/all-MiniLM-L6-v2
  index.py             # FAISS IndexFlatL2 + metadata.json sidecar
  build.py             # CLI: python -m rag.build --corpus ./corpus/
corpus/                # seed documents (.md / .txt)
  README.md            # the project README copied in as a starter chunk source
  ...
```

### Behavior

- Index loaded **once** at worker startup; held in memory for process lifetime.
- Two-hop retrieval enforced (single hop rejected internally, query reformulated, retried).
- Each chunk tagged: `chunk_id`, `source_url`, `relevance_score`, `hop_number`.
- `LOW_COVERAGE` flag set in `SharedContext` when no chunk passes the relevance threshold (default 0.7, configurable via `RAG_RELEVANCE_THRESHOLD`).

### Acceptance criteria

1. `python -m rag.build --corpus ./corpus/` produces `index.faiss` + `metadata.json` in `<output_dir>`.
2. Worker startup loads the index; `/healthz` extends with `{"rag":"ok","chunks_loaded":<int>}`.
3. A query for a fact in the corpus returns chunks with both `hop_number=1` and `hop_number=2` in the provenance.
4. A query for an out-of-corpus fact sets `LOW_COVERAGE=true` in the final answer's metadata.

### Defer

- Live ingestion pipeline that watches S3/webhook (post-extended #1).
- pgvector alternative (deferred unless FAISS proves insufficient).

---

## Slice E5 — Full SSE event coverage + token-level streaming

**Builds on:** Slice `0–1` (`token`, `agent_start`, `agent_end`, `job_complete` events live).

**Outcome:** All 8 event types from README §Streaming & Observability emitted at the correct boundaries; LLM streaming forwards one event per real LLM token.

### Event map (verbatim from README)

| Event | Emitted by | Payload |
|---|---|---|
| `agent_start` | `AgentBase.__aenter__` | `{agent_id, budget_remaining}` |
| `token` | LLM stream forwarder | `{agent_id, text}` — one per LLM token |
| `tool_call_start` | `with_retry` decorator entry | `{tool_name, input_hash}` |
| `tool_call_end` | `with_retry` decorator exit | `{tool_name, latency_ms, success}` |
| `budget_update` | `ContextBudgetManager.consume` | `{agent_id, tokens_used, tokens_remaining}` |
| `agent_end` | `AgentBase.__aexit__` | `{agent_id, output_hash, policy_violations}` |
| `job_complete` | Worker task finalizer | `{job_id, total_latency_ms}` |
| `error` | Global exception handler | `{error_code, message, job_id}` |

### Files

```
backend/app/sse_emitter.py    # central emit(job_id, event); used by all agents/tools/manager
```

LLM streaming change: replace `client.messages.create(...)` with `client.messages.stream(...)` and forward each text delta as a `token` event.

### Acceptance criteria

1. A live multi-sentence query produces ≥50 `token` events (real per-token streaming, not phrase-level).
2. Every tool invocation has matched `tool_call_start` / `tool_call_end` events with hash-equal `input_hash`.
3. `consume()` emits `budget_update` after every successful append.
4. Forcing an `OrchestratorError` produces exactly one `error` event with non-empty `error_code`, `message`, `job_id`.
5. Frontend (slice `3.5–4.5`) renders all 8 event types distinctly (color-coded by `event.type`).

---

## Slice E6 — Full `/trace` and `/evals` endpoints

**Builds on:** Slice `4.5–5.5` (eval tables populated) and slice `E5` (full event log).

**Outcome:** Observability and eval reporting endpoints match README §API Reference exactly.

### New / extended endpoints

| Endpoint | Method | Response shape |
|---|---|---|
| `/trace/{job_id}` | GET | `{job_id, status, events[], tool_calls[], routing_plan, final_answer}` |
| `/evals/latest` | GET | `{run_id, run_at, categories: {baseline, ambiguous, adversarial}, overall}` per-dimension averages |
| `/evals/diff?run_a=&run_b=` | GET | `{per_dimension_deltas: {...}, regression_cases: [...]}` |

### Behavior

- `/trace/{job_id}` aggregates from `agent_logs` + `tool_calls` + `jobs`. Returns 404 if job not found, 202 with partial trace if `status='RUNNING'`.
- `/evals/latest` is a single SQL query against `eval_runs` ordered by `run_at DESC LIMIT 1`, joined to `eval_cases` aggregated per category.
- `/evals/diff` computes per-dimension deltas; flags any dimension dropping > 0.05 as `regression_cases`.

### Acceptance criteria

1. After a job completes, `/trace/{id}` returns a complete trace including the `RoutingPlan` JSON.
2. After 2 eval runs, `/evals/diff?run_a=A&run_b=B` returns non-empty `per_dimension_deltas`.
3. Calling `/trace/{id}` while job is still running returns HTTP 202 with whatever events have been logged so far.

---

## Slice E7 — Full eval coverage (15 cases, 6 dimensions)

**Builds on:** Slice `4.5–5.5` (5-case 3-dimension eval running).

**Outcome:** Full README §Evaluation Pipeline coverage. 15 cases across 3 categories, 6 scoring dimensions.

### Cases (5 per category)

| Category | Count | Examples |
|---|---|---|
| Baseline | 5 | Simple factual / analytical queries with deterministic gold answers |
| Ambiguous | 5 | "Explain the impact" (no subject); "Compare them" (no antecedent); etc. |
| Adversarial | 5 | 1 prompt-injection + 1 false-premise + 1 contradiction-trigger + 1 underspecified-with-redirect + 1 multi-step-with-bait |

### Scoring dimensions (all 6)

| Dimension | Already in MVP? | Implementation note |
|---|---|---|
| `answer_correctness` | ✅ | Semantic similarity (cosine) for prose; exact match for structured |
| `citation_accuracy` | ✅ | Fraction of citations that resolve to a real, relevant chunk |
| `critique_agreement` | ✅ | Fraction of final-output sentences with `SUPPORTED` verdict |
| `contradiction_resolution` | ❌ → E7 | Whether all Critique flags were resolved in final output (boolean → 1.0/0.0 per case, averaged) |
| `tool_efficiency` | ❌ → E7 | `1 − (unnecessary_tool_calls / total_tool_calls)` — "unnecessary" = `accepted_by_agent=False AND no fallback triggered` |
| `budget_compliance` | ❌ → E7 | `1.0 − 0.1 × policy_violation_count` (clamped to [0, 1]) |

### Files

```
eval/
  cases/
    baseline_01.yaml    ... baseline_05.yaml
    ambiguous_01.yaml   ... ambiguous_05.yaml
    adversarial_01.yaml ... adversarial_05.yaml
  scorer.py             # extended with 3 new dimension scorers
  run.py                # extended: --cases filter, --concurrency arg, --threshold arg
```

Each case file:
```yaml
id: baseline_01
category: baseline
query: "What is the retry policy for tools?"
gold_answer: "Up to 2 retries per tool, then per-tool fallback fires unconditionally."
expected_citations: ["readme_retry_policy"]
acceptance:
  answer_correctness: ">= 0.8"
  citation_accuracy: ">= 0.5"
```

### Reproducibility

- `run_hash` = SHA-256 of (sorted_case_ids + agent_prompt_versions + tool_versions + corpus_hash). Re-running with identical inputs produces identical `run_hash`.

### Acceptance criteria

1. `python -m eval.run` produces 1 `eval_run` row + 15 `eval_cases` rows in <90 s with `EVAL_CONCURRENCY=4`.
2. `/evals/latest` returns per-category averages for **all 6 dimensions**.
3. `run_hash` is stable across two consecutive runs with no code changes.
4. Adversarial prompt-injection case scores `answer_correctness ≥ 0.7` (system refused the injection).

---

## Slice E8 — Meta-Agent + prompt rewrite generation

**Builds on:** Slice `E7` (full 6-dimension eval data exists).

**Outcome:** Post-eval analysis identifies the worst-scoring prompt and proposes a single rewrite per run, stored as PENDING.

### Files

```
backend/app/agents/meta.py              # MetaAgent.analyze(eval_run_id) -> PromptRewrite | None
backend/app/services/prompt_rewrites.py # CRUD on prompt_rewrites table
backend/app/agents/prompt_loader.py     # central prompt registry (target_prompt_id → text)
```

### Behavior (verbatim from README §Self-Improving Prompt Loop)

- Triggered automatically after `eval/run.py` completes (or `/eval/rerun`).
- Reads `eval_cases WHERE score < threshold` (default 0.6, configurable via `META_THRESHOLD`).
- Ranks prompts by **worst average dimension score**.
- Produces **exactly one** `PromptRewrite` per eval run with:
  - `target_prompt_id`
  - `original_text` (verbatim from prompt registry)
  - `proposed_text` (the rewrite)
  - `unified_diff` (computed via Python `difflib.unified_diff`)
  - `justification` (LLM-generated, ≤200 words)
  - `expected_dimension_delta` (`{"answer_correctness": +0.05, ...}`)
- Status: `PENDING`. **Does not auto-apply.**

### Constraints

- Max 1 rewrite per eval run (enforced by unique partial index on `(target_prompt_id, status='PENDING')`).
- Does not run during an ongoing job.

### Acceptance criteria

1. After a low-scoring eval run (force `META_THRESHOLD=1.0` to ensure failures), exactly 1 row in `prompt_rewrites` with `status='PENDING'` and a non-empty `unified_diff`.
2. Re-running eval without approval does not create a duplicate PENDING rewrite for the same prompt.
3. `justification` field is non-empty and references at least one specific failed `case_id`.

---

## Slice E9 — Approval endpoints + targeted re-eval + performance deltas

**Builds on:** Slice `E8` (Meta-Agent producing PENDING rewrites).

**Outcome:** Human-in-the-loop closes the self-improvement loop; deltas measured and stored.

### New endpoints

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/rewrites/{id}/approve` | POST | `{"approved_by":"<str>"}` | `204` |
| `/rewrites/{id}/reject` | POST | `{"rejected_by":"<str>","reason":"<str>"}` | `204` |
| `/eval/rerun` | POST | `{"min_score_threshold":0.6}` | `202 {"rerun_job_id":"<uuid>","case_count":<int>}` |

### Behavior

- **Approve:** sets `status='APPROVED'`, `decided_at=now()`, `decided_by=approved_by`. Reloads `prompt_loader` registry so subsequent jobs use the new text. **Does NOT modify any in-flight job.**
- **Reject:** sets `status='REJECTED'`, `reject_reason`, `decided_at`, `decided_by`. Archives.
- **Re-eval:** finds all `eval_cases` from latest run with any dimension score below `min_score_threshold`; reruns ONLY those cases; stores results in a new `eval_run`.
- After re-eval completes, write one `performance_deltas` row:
  ```jsonc
  {
    "rewrite_id": "...",
    "case_ids": ["...", "..."],
    "before_scores": {"baseline": {...}, "ambiguous": {...}},
    "after_scores":  {...},
    "delta_scores":  {...},
    "rerun_at": "ISO-8601"
  }
  ```

### Constraints

- `REWRITE_DECIDED` (HTTP 409) if rewrite is already approved or rejected.
- `REWRITE_NOT_FOUND` (HTTP 404) on unknown id.
- Rollback on regression is **NOT automatic** — flagged as a Known Limitation in README. Human reviews the delta and may approve a counter-rewrite.

### Acceptance criteria

1. Approve a pending rewrite → `prompt_rewrites.status='APPROVED'`, `decided_by` set, `decided_at` set.
2. Trigger `/eval/rerun` → re-eval runs only on cases that scored below threshold; new `eval_run` + `performance_deltas` row written.
3. `delta_scores` shows non-zero values across at least one dimension.
4. Calling approve twice → second call returns `409 REWRITE_DECIDED`.

---

## Slice E10 — Datasette log UI

**Builds on:** All MVP + slices `E1–E9` (every table populated with realistic data).

**Outcome:** Read-only browser interface on `http://localhost:8080` for `agent_logs`, `tool_calls`, `eval_runs`, `eval_cases`, `prompt_rewrites`, `performance_deltas`, `compressed_sidecars`.

### Files

```
logui/
  Dockerfile           # python:3.12-slim + datasette + datasette-postgresql
  metadata.yml         # table descriptions + canned queries + hidden columns
  start.sh             # entry script: datasette serve postgresql://mega_ro@db:5432/mega
docker-compose.yml     # add `logui` service on port 8080
```

### Database role

```sql
CREATE ROLE mega_ro LOGIN PASSWORD '<from env>';
GRANT CONNECT ON DATABASE mega TO mega_ro;
GRANT USAGE ON SCHEMA public TO mega_ro;
GRANT SELECT ON ALL TABLES IN SCHEMA public TO mega_ro;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT SELECT ON TABLES TO mega_ro;
```

### Canned queries (in `metadata.yml`)

- "Latest eval run summary" — most recent `eval_runs` joined to per-category averages.
- "Pending rewrites" — `prompt_rewrites WHERE status='PENDING'`.
- "Slowest tools last 24h" — `tool_calls` ordered by `latency_ms DESC LIMIT 50`, where `called_at > now() - interval '24 hours'`.
- "Policy violations last 7d" — `agent_logs WHERE policy_violations IS NOT NULL`.
- "Performance deltas timeline" — `performance_deltas` ordered by `rerun_at DESC`.

### Acceptance criteria

1. `http://localhost:8080` loads with all 7 tables visible and queryable.
2. Canned query "Latest eval run summary" returns the most recent run's per-category breakdown.
3. Datasette user **cannot** execute `INSERT/UPDATE/DELETE` (verify by attempting a custom SQL query).
4. `agent_logs` table view hides `input_hash` and `output_hash` columns (still queryable, just not shown by default).

---

## Slice E11 — Auth, rate limiting, request-ID propagation

**Builds on:** All prior slices.

**Outcome:** Endpoints gated by API key. Basic abuse protection. Per-request tracing.

### Files

```
backend/app/auth.py          # APIKeyMiddleware reading X-API-Key header
backend/app/ratelimit.py     # Redis-backed sliding window limiter
backend/app/middleware.py    # X-Request-ID propagation; structured access log
```

### Behavior

- All endpoints **require** `X-API-Key` header EXCEPT `/healthz` and (optionally) `/static/*`.
- Default limit: 60 requests/min per key (configurable via `RATE_LIMIT_PER_MIN`). Sliding window in Redis using key `rl:<api_key_hash>:<window>`.
- Every request gets an `X-Request-ID` (generated if absent); propagated to all log lines and ARQ task arguments.

### Errors

| Condition | HTTP | Body |
|---|---|---|
| Missing key | 401 | `{"error":"AUTH_MISSING"}` |
| Invalid key | 401 | `{"error":"AUTH_INVALID"}` |
| Rate limited | 429 | `{"error":"RATE_LIMITED","retry_after_seconds":<int>}` |

### Acceptance criteria

1. Request without `X-API-Key` → 401 `AUTH_MISSING`.
2. Request with bogus key → 401 `AUTH_INVALID`.
3. 61st request within 60 s from one valid key → 429 with `Retry-After` header.
4. `X-Request-ID` set on all responses; same ID appears in `agent_logs` rows for that job.
5. `/healthz` remains accessible without a key.

### Defer

- RBAC (`reader`/`operator`/`auditor` roles) — post-extended #8.
- JWT instead of API keys — out of scope.

---

# Post-extended (out of scope even for extended)

These map directly to README §What We Would Build Next. Document them in the README; do not implement.

| # | Item | Why deferred |
|---|---|---|
| 1 | **Live document ingestion pipeline** (S3/webhook → embed → upsert FAISS without restart) | Requires a separate watcher service + index hot-swap protocol; substantial scope |
| 2 | **Automated red-teaming loop** (sixth agent generating novel adversarial queries each cycle) | Requires careful safety guardrails to avoid generating actual harmful prompts |
| 3 | **Full gVisor / Firecracker sandbox** for `code_exec` | Requires platform-specific infra (Linux kernel features, KVM access); not Docker-friendly |
| 4 | **Per-agent model routing** (small/fast for self-reflection; frontier for synthesis) | Requires per-agent cost/latency telemetry to drive routing decisions |
| 5 | **Streaming eval results** (SSE-stream partial eval-run results) | Requires eval pipeline refactor to emit incremental events |
| 6 | **Prompt rewrite simulation pre-approval** (shadow-run failure cases, surface predicted delta) | Requires deterministic shadow-execution path; meaningful work |
| 7 | **OpenTelemetry / Jaeger integration** (replace `agent_logs` with OTel spans) | Requires Jaeger/Tempo deployment; replaces a working table-based system |
| 8 | **RBAC** (`reader` / `operator` / `auditor` roles) | Requires identity provider integration; outside assessment scope |

Reference these in `README.md §What We Would Build Next` with one-line justifications. Naming them is a signal of judgment, not omission.

---

## Pre-flight scope decisions (lock before coding)

| Question | Decision |
|---|---|
| What does "deliver" mean? | _(Darq to confirm: demo recording + repo + brief writeup?)_ |
| Single demoable user moment | User submits a query → watches agents stream reasoning live → gets a cited answer with provenance map. |
| Acceptable fakes (MVP only) | Hardcoded RAG corpus. Stub agent in slice 0–1 only. Sequential agent execution (no dependency FSM in MVP). 1-retry tool policy. |
| Stop condition for hour 6 (MVP) | Working `docker compose up` + recorded demo + clean README + 1 commit per slice. **Not** "feature-complete." |
| Stop condition for extended | All `E1–E11` acceptance criteria green; README updated to reflect extended state; eval coverage at 15 cases × 6 dimensions. |

---

## Handoff back to PM

Call John (`bmad-agent-pm <question>`) when:
- A slice's acceptance criteria are slipping and you need to decide what to cut.
- A new requirement appears mid-slice that doesn't fit the plan.
- You finish slice `5.5–6` (MVP) and want a submission-readiness check.
- You finish any extended slice and want to decide which `E_n` to take next.
