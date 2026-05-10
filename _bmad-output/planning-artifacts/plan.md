# 6-Hour Assessment Plan

**Project:** Real-Time Multi-Agent LLM Orchestration & Evaluation System
**Spec source:** `README.md` (treat as PRD + Architecture)
**Owner:** Darq · **Sequencing:** John (PM)
**Started:** 2026-05-10

---

## Guiding principle

The smallest unit isn't *below* the backend — it's *across* it. A vertical slice that streams one token end-to-end through `FastAPI → worker → SharedContext → SSE → client` is the spine. Everything else is reps on the spine.

Cut deliberately, not by hour-5 panic.

---

## Glossary — what "fake / stub / mock" means here

In slice 0–1 we wire the **runtime** before we make it intelligent. Real wiring, fake brains.

| Term | Meaning | Example |
|---|---|---|
| **Stubbed agent** | A real Python class implementing the agent interface, but its `run()` returns hardcoded output instead of calling an LLM. | `StubAgent.run()` yields `["Routing query.", "Looking up.", "Done."]` with 200 ms sleep between each. No `LLM_API_KEY` needed. |
| **Mock tokens** | SSE `token` events emitted from a hardcoded list, not real LLM streaming. Same event shape as the real ones — client cannot tell the difference. | `yield SSEEvent(type="token", agent_id="stub", text=word)` in a loop. |
| **Fake corpus** *(slice 1–2.5)* | 5–10 hardcoded text chunks in a Python list. RAG retrieves by keyword match, not vectors. | `CORPUS = [{"chunk_id": "c1", "text": "...", "source_url": "..."}]` |

Each fake gets replaced in a later slice. Track replacements in commit messages.

---

## Agent runbook

| Agent | Command (verbatim) | What you'll get back |
|---|---|---|
| **John** (PM) | `bmad-agent-pm <question>` | Scope decisions, cut calls, sequencing. Returns when scope drifts. |
| **Winston** (Architect) | `bmad-agent-architect <prompt>` | Design docs, ADRs, file structure choices. **Does not write production code.** |
| **Amelia** (Dev) | `bmad-agent-dev <prompt>` | Code execution against a story file. Use when you've created a story via `bmad-create-story`. |
| **Quick Dev** (no persona) | `bmad-quick-dev <prompt>` | Direct intent-to-code. Skips story ceremony. **Fastest path, recommended for this assessment.** |

Always start each agent in a **fresh context window**. Paste the prompt right after the command name.

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

**Skip:** the BMad orthodox flow (`bmad-create-story` → `bmad-agent-dev`). Story ceremony costs 30–45 min for a slice this small. Save Amelia/stories for slice 1–2.5 where real agent logic lands.

---

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

The `SSEEvent` discriminated union must be defined in `models.py` so slice 1–2.5's real agents drop in without changing the wire format.

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

## Deliberately NOT being built (call this out in the submission)

These are flagged in the README as out-of-scope-for-time. Naming them is judgment, not omission.

- Meta-Agent / self-improvement prompt loop · `prompt_rewrites` endpoint pair · `performance_deltas` re-eval
- Datasette log UI (replaced by `GET /trace/{job_id}` + Postgres)
- Compression Agent + budget overflow → compressed-context flow
- Full 2-retry tool FSM (we ship 1 retry)
- `code_exec` and `sql_lookup` tools (only `self_reflection`)
- Auth on endpoints

Move all of these into README §What We Would Build Next with one-line justifications.

---

## Pre-flight scope decisions (lock before coding)

| Question | Decision |
|---|---|
| What does "deliver" mean? | _(Darq to confirm: demo recording + repo + brief writeup?)_ |
| Single demoable user moment | User submits a query → watches agents stream reasoning live → gets a cited answer with provenance map. |
| Acceptable fakes | Hardcoded RAG corpus. Stub agent in slice 0–1 only. Sequential agent execution (no dependency FSM). 1-retry tool policy. |
| Stop condition for hour 6 | Working `docker compose up` + recorded demo + clean README + 1 commit per slice. **Not** "feature-complete." |

---

## Handoff back to PM

Call John (`bmad-agent-pm <question>`) when:
- A slice's acceptance criteria are slipping and you need to decide what to cut.
- A new requirement appears mid-slice that doesn't fit the plan.
- You finish slice 5.5–6 and want a submission-readiness check.
