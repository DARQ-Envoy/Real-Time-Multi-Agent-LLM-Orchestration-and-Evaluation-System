---
title: 'Slice 0-1: Vertical spine — FastAPI + Postgres + ARQ + Redis + StubAgent SSE'
type: 'feature'
created: '2026-05-10'
status: 'in-progress'
baseline_commit: 'NO_COMMITS'
context:
  - '{project-root}/_bmad-output/planning-artifacts/plan.md'
  - '{project-root}/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** No runtime exists. Later slices need a proven spine — POST → queue → worker → SSE — before any real LLM logic lands. Without it, nothing else can be demoed.

**Approach:** Wire FastAPI + asyncpg + ARQ + Redis + Postgres in Docker Compose. A single `StubAgent` returns hardcoded tokens via the same SSE event shape real agents will use later, so the wire format never changes.

## Boundaries & Constraints

**Always:**
- Schema in `001_init.sql` is verbatim from `README.md` §Database Schema (7 tables: jobs, agent_logs, tool_calls, eval_runs, eval_cases, prompt_rewrites, performance_deltas). The `pgcrypto` extension must be enabled so `gen_random_uuid()` resolves.
- SSE events use the `SSEEvent` discriminated union in `models.py`. StubAgent emits exactly: `agent_start` → 3× `token` → `agent_end` → `job_complete`.
- Redis is both ARQ broker AND the SSE pub/sub bus. Worker publishes events on channel `job:<job_id>`; the `/stream/{job_id}` endpoint subscribes.
- All config via `pydantic-settings`. No hardcoded URLs/keys.
- The worker writes the final answer JSON and `status='COMPLETE'` to `jobs` before publishing `job_complete`.

**Ask First:**
- Adding any tool, RAG, real LLM call, or second agent (that's Slice 1-2.5).
- Changing the SSE event shape or the SQL schema.

**Never:**
- No real LLM calls in this slice. `LLM_API_KEY` is read but unused.
- No FAISS, no vector store, no decomposition logic.
- No auth, no Datasette, no eval harness yet.
- No ORM. asyncpg only.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Healthcheck happy path | GET /healthz, db+redis up | 200 `{"status":"ok","db":"ok","redis":"ok"}` | n/a |
| Healthcheck degraded | DB or Redis ping fails | 503 with the failing subsystem set to error string | catch and report, don't crash |
| Submit query | POST /query `{"query":"hello"}` | 202 `{"job_id":"<uuid>","stream_url":"/stream/<uuid>"}`; jobs row inserted with status=QUEUED; ARQ job enqueued | n/a |
| Empty query | POST /query `{"query":""}` | 422 `{"error_code":"QUERY_EMPTY"}` | Pydantic validator |
| Stream a known job | GET /stream/{job_id} immediately after POST | text/event-stream emits ≥5 `data:` lines (start + 3 tokens + end + complete) within 5 s, then closes | Subscriber starts before worker publishes; use Redis pub/sub with replay-from-list fallback so events queued before subscribe aren't lost |
| Stream unknown job | GET /stream/<random-uuid> | 404 `{"error_code":"JOB_NOT_FOUND"}` | check jobs table first |
| Worker exception | StubAgent raises | jobs.status=FAILED, `error` SSE event published, `job_complete` still emitted so client unblocks | try/except around run_query |

</frozen-after-approval>

## Code Map

- `backend/app/main.py` -- FastAPI app: lifespan (db pool, redis), GET /healthz, POST /query, GET /stream/{job_id}
- `backend/app/settings.py` -- pydantic-settings (DATABASE_URL, REDIS_URL, LLM_API_KEY, MAX_BUDGET_TOKENS)
- `backend/app/db.py` -- asyncpg pool create/close
- `backend/app/redis_bus.py` -- publish_event / subscribe helpers; uses both pub/sub AND a Redis list `job:<id>:events` for replay
- `backend/app/models.py` -- Pydantic v2: SharedContext, RoutingPlan, ToolResult, AnswerSegment, JobRequest, JobResponse, SSEEvent (discriminated union by `type`)
- `backend/app/agents/base.py` -- AgentBase ABC with `async run(ctx) -> AsyncIterator[SSEEvent]`
- `backend/app/agents/stub.py` -- StubAgent yields hardcoded events with 200 ms sleeps
- `backend/app/worker.py` -- ARQ WorkerSettings + `run_query(ctx, job_id, query)` task
- `backend/app/sql/001_init.sql` -- README schema verbatim + `CREATE EXTENSION IF NOT EXISTS pgcrypto`
- `backend/app/bootstrap.py` -- runs 001_init.sql idempotently on api startup
- `backend/Dockerfile` -- python:3.12-slim, non-root user, single image used by both api and worker (different command)
- `backend/requirements.txt` -- fastapi, uvicorn[standard], asyncpg, arq, redis, pydantic, pydantic-settings
- `docker-compose.yml` -- api, worker, db, redis with healthchecks and depends_on conditions
- `.env.example` -- documents every variable with safe defaults

## Tasks & Acceptance

**Execution:**
- [ ] `backend/requirements.txt` -- pin top-level deps, no transitive pins
- [ ] `backend/app/sql/001_init.sql` -- copy schema verbatim from README §Database Schema, prepend `CREATE EXTENSION IF NOT EXISTS pgcrypto;`
- [ ] `backend/app/settings.py` -- Settings class loading env via pydantic-settings
- [ ] `backend/app/models.py` -- define all Pydantic models; SSEEvent is `Annotated[Union[...], Field(discriminator="type")]`
- [ ] `backend/app/db.py` -- async pool factory + module-level singleton accessor
- [ ] `backend/app/redis_bus.py` -- publish (LPUSH list + PUBLISH channel), subscribe (LRANGE replay then SUBSCRIBE for new), JSON serialize SSEEvent
- [ ] `backend/app/agents/base.py` + `stub.py` -- StubAgent emits the exact 6-event sequence from plan.md
- [ ] `backend/app/worker.py` -- ARQ task: insert jobs row → run StubAgent → publish each event → write final_answer + status=COMPLETE → publish job_complete
- [ ] `backend/app/bootstrap.py` -- on api startup, run 001_init.sql if `jobs` table missing
- [ ] `backend/app/main.py` -- lifespan(db, redis), /healthz, /query (enqueue via arq.connections.create_pool), /stream (StreamingResponse iterating subscribe())
- [ ] `backend/Dockerfile` -- multi-stage, non-root, COPY app, default CMD runs uvicorn; worker overrides CMD
- [ ] `docker-compose.yml` -- 4 services with healthchecks; api `depends_on` db+redis healthy; worker same
- [ ] `.env.example` -- all vars from README §Configuration with assessment-safe defaults

**Acceptance Criteria:**
- Given a clean checkout, when `docker compose up --build` runs, then all four containers stay up ≥30 s.
- Given the stack is up, when `curl http://localhost:8000/healthz`, then 200 with `db` and `redis` both `"ok"`.
- Given the stack is up, when `docker compose exec db psql -U mega -d mega -c '\dt'`, then output lists all 7 tables.
- Given the stack is up, when `curl -X POST http://localhost:8000/query -H 'content-type: application/json' -d '{"query":"hello"}'`, then 202 with `job_id` (UUID) and `stream_url`.
- Given a fresh `job_id`, when `curl -N http://localhost:8000/stream/<job_id>` is run within 5 s of the POST, then ≥5 `data: ...` lines arrive (start + 3 tokens + end + complete).
- Given the stream finished, when querying `jobs` for that id, then `status='COMPLETE'` and `final_answer` is non-null JSONB.

## Spec Change Log

## Design Notes

**SSE-via-Redis-pub-sub with list replay:** A subscriber that joins after the publisher started would miss events. Solution: publisher does both `LPUSH job:<id>:events <json>` (+ `EXPIRE 600`) and `PUBLISH job:<id> <json>`. Subscriber first `LRANGE` the list (replays in order), then opens a pub/sub for new ones. Stop condition: `job_complete` event seen.

**StubAgent output sequence (literal):**
```
agent_start{agent_id:"stub", budget_remaining:4096}
token{agent_id:"stub", text:"Routing query."}
token{agent_id:"stub", text:"Looking up."}
token{agent_id:"stub", text:"Done."}
agent_end{agent_id:"stub", output_hash:sha256("Routing query.Looking up.Done."), policy_violations:null}
job_complete{job_id, total_latency_ms}
```

**Single image for api + worker:** Reduces build time and keeps deps in sync. `command:` in compose differentiates: `uvicorn app.main:app` vs `arq app.worker.WorkerSettings`.

## Verification

**Commands:**
- `docker compose up --build -d` -- expected: 4 containers Up
- `curl http://localhost:8000/healthz` -- expected: HTTP 200, JSON has all subsystems "ok"
- `docker compose exec db psql -U mega -d mega -c '\dt'` -- expected: 7 tables listed
- `curl -X POST http://localhost:8000/query -H 'content-type: application/json' -d '{"query":"hello"}'` -- expected: HTTP 202 with job_id
- `curl -N http://localhost:8000/stream/<job_id>` -- expected: ≥5 SSE data lines, terminates after job_complete
- `docker compose exec db psql -U mega -d mega -c "SELECT status, final_answer IS NOT NULL FROM jobs ORDER BY created_at DESC LIMIT 1;"` -- expected: COMPLETE, t
