---
title: 'Slice E1: Full tool layer — web_search + code_exec + sql_lookup, Orchestrator-driven dispatch'
type: 'feature'
created: '2026-05-11'
status: 'done'
baseline_commit: '768b2c9b4f1d7fc5d230f29cec924769abf58811'
context:
  - '{project-root}/_bmad-output/planning-artifacts/extended-plan.md'
  - '{project-root}/README.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** Only `self_reflection` is wired through `run_with_retry`. README §Tool Catalogue requires four tools (`web_search`, `code_exec`, `sql_lookup`, `self_reflection`) reachable by agents with documented failure modes — three are missing, and the Orchestrator has no way to route to them.

**Approach:** Add three tool modules that conform to the existing `ToolFn` signature `(SharedContext, LLMClient) -> ToolResult`, expose them via a `registry`, extend `RoutingPlan` with `tool_calls: list[PlannedToolCall]`, teach the Orchestrator's `emit_routing_plan` schema to populate it, and have `pipeline.py` dispatch each planned tool via `run_with_retry` (still `max_retries=1`) before the dependent agent runs.

## Boundaries & Constraints

**Always:**
- New tool modules MUST use signature `async def run(ctx: SharedContext, llm: LLMClient) -> ToolResult` (mirrors `tools/self_reflection.py`) so they drop into `run_with_retry` unchanged.
- Every tool invocation flows through `tools/runner.run_with_retry(...)` — never call a tool directly from `pipeline.py`. This is what populates `tool_calls` rows and emits `tool_call_start/end` SSE.
- Missing/stub env keys MUST return a `ToolResult(success=False, error_code="MALFORMED", error_message=...)` synchronously — never raise, never make a network call.
- `code_exec` banned-import check uses `ast.parse` walker (top-level `Import`/`ImportFrom`), never regex.
- `sql_lookup` connects via a SECOND asyncpg pool bound to a NEW Postgres role `mega_ro` that has only `SELECT` privileges; reject any LLM-emitted SQL whose first non-comment token is not `SELECT` (case-insensitive).
- The four `error_code` values from README must be used verbatim: `TIMEOUT`, `EMPTY`, `MALFORMED`, `EXEC_ERROR`. No new codes.
- Tavily key sentinels treated as "not configured": empty string, `stub-not-used-in-slice-0`.

**Ask First:**
- Any change to `tools/runner.py` or to `MAX_RETRIES_DEFAULT` — that is Slice E2, not E1.
- Adding a Brave / SerpAPI provider, or any abstraction over `web_search`.
- Replacing the subprocess sandbox with a real isolation layer (gVisor/Firecracker is post-extended #3).

**Never:**
- Do not introduce `tools/fallbacks.py` or per-tool fallback contracts — that is Slice E2.
- Do not add a `ContextBudgetManager` gate on tool outputs — that is Slice E3.
- Do not modify `tool_calls` table schema; existing columns are sufficient.
- Do not call `python` on the host to test `code_exec` — exec inside the worker container (POSIX subprocess semantics).
- Do not let an `httpx` error or `subprocess.TimeoutExpired` propagate past the tool function.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| `web_search` happy | `TAVILY_API_KEY` set; Tavily returns 3 results | `ToolResult(success=True, data=[{title,url,snippet,relevance_score}, ...])` | N/A |
| `web_search` TIMEOUT | `httpx` raises `TimeoutException` | `success=False, error_code="TIMEOUT"` | swallow exception |
| `web_search` EMPTY | Tavily returns `{"results": []}` | `success=False, error_code="EMPTY", data=[]` | N/A |
| `web_search` MALFORMED (no key) | `TAVILY_API_KEY` unset or `stub-not-used-in-slice-0` | `success=False, error_code="MALFORMED"` — no HTTP call | synchronous |
| `web_search` MALFORMED (bad body) | HTTP 4xx with non-JSON body | `success=False, error_code="MALFORMED"` | swallow exception |
| `code_exec` happy | `code="print(2+2)"` | `success=True, data={"stdout":"4\n","stderr":"","exit_code":0}` | N/A |
| `code_exec` TIMEOUT | `code="while True: pass"`, `CODE_EXEC_TIMEOUT_SECONDS=1` | `success=False, error_code="TIMEOUT"` | `subprocess.TimeoutExpired` caught; child killed |
| `code_exec` MALFORMED | `code="import os"` (or `subprocess`, `socket`, `urllib`, `urllib3`, `requests`, `httpx`, `ctypes`, `pathlib`, `shutil`, `builtins`, `importlib`) | `success=False, error_code="MALFORMED"` — AST walker rejects pre-spawn | no subprocess spawned |
| `code_exec` EMPTY | `code="pass"` → exit 0, empty stdout & stderr | `success=False, error_code="EMPTY"` | N/A |
| `code_exec` EXEC_ERROR | non-zero exit with stderr (e.g. `raise ValueError`) | `success=False, error_code="EXEC_ERROR", error_message=<stderr>` | N/A |
| `sql_lookup` happy | NL query → LLM emits `SELECT id FROM jobs LIMIT 1` | `success=True, data={columns, rows, sql}` | N/A |
| `sql_lookup` MALFORMED (non-SELECT) | LLM emits `DROP TABLE jobs;` | `success=False, error_code="MALFORMED", error_message=<sql>` | rejected before execute |
| `sql_lookup` TIMEOUT | `SET LOCAL statement_timeout='1s'`; Cartesian join | `success=False, error_code="TIMEOUT"` | asyncpg query cancelled |
| `sql_lookup` EXEC_ERROR | Valid `SELECT` against missing column | `success=False, error_code="EXEC_ERROR", error_message=<sql>` | swallow exception |

</frozen-after-approval>

## Code Map

- `backend/app/tools/runner.py` — `run_with_retry(...)`; UNTOUCHED. Tools plug into this.
- `backend/app/tools/self_reflection.py` — reference signature for new tools; UNTOUCHED.
- `backend/app/tools/__init__.py` — currently empty; convert to re-export `registry`.
- `backend/app/tools/registry.py` — NEW: `REGISTRY: dict[str, ToolFn]` mapping `"web_search"`, `"code_exec"`, `"sql_lookup"`, `"self_reflection"` → callables; `lookup(name)` raises on miss.
- `backend/app/tools/web_search.py` — NEW: Tavily client (`httpx.AsyncClient`, 5s timeout) + Tavily→README schema mapping; key-sentinel guard.
- `backend/app/tools/code_exec.py` — NEW: `ast.parse` banned-import walker + `subprocess.run([sys.executable,"-I","-S","-c",code], env={}, capture_output=True, text=True, timeout=settings.CODE_EXEC_TIMEOUT_SECONDS)`.
- `backend/app/tools/sql_lookup.py` — NEW: LLM `emit_sql` tool-call → `SELECT` prefix check → `mega_ro` pool with `SET LOCAL statement_timeout='8s'`.
- `backend/app/models.py` — extend: add `PlannedToolCall(agent_id, tool_name, input: dict)`; add `RoutingPlan.tool_calls: list[PlannedToolCall] = []`.
- `backend/app/agents/orchestrator.py` — extend `ROUTING_PLAN_TOOL.input_schema.properties` with `tool_calls` array; parse into `plan.tool_calls`; `DEFAULT_FALLBACK_PLAN` keeps `tool_calls=[]`.
- `backend/app/agents/prompts/orchestrator.md` — describe the 4 tools and when to invoke each (one paragraph per tool, ≤ ~30% prompt growth).
- `backend/app/pipeline.py` — between each agent run, dispatch every `ctx.routing_plan.tool_calls` whose `agent_id == <current_agent>` via `run_with_retry`; append each `ToolResult.data` to `ctx.agent_outputs["tools"][tool_name]` (list).
- `backend/app/settings.py` — add `TAVILY_API_KEY: str = ""`, `MEGA_RO_PASSWORD: str = "mega_ro"`.
- `backend/app/db.py` — add `create_ro_pool()` returning a second `asyncpg.Pool` bound to `postgresql://mega_ro:<MEGA_RO_PASSWORD>@db:5432/mega`.
- `backend/app/bootstrap.py` — also run `003_readonly_role.sql` after `001_init.sql`.
- `backend/app/sql/003_readonly_role.sql` — NEW: idempotent `CREATE ROLE mega_ro` (with `LOGIN`, password from session `app.mega_ro_password` set via `SET` or use psql variable interpolation; see Design Notes), `GRANT SELECT ON ALL TABLES IN SCHEMA public`, `ALTER DEFAULT PRIVILEGES ... GRANT SELECT`.
- `backend/app/worker.py` / `backend/app/main.py` — open the RO pool at startup, pass it through job execution where `sql_lookup` can reach it. Simplest: store it on a module-level handle initialized in worker startup, imported by `sql_lookup.py`.
- `backend/requirements.txt` — pin `httpx>=0.27,<1` (already transitively present via `anthropic`, but make explicit).
- `.env.example` — add `TAVILY_API_KEY=`, `MEGA_RO_PASSWORD=mega_ro`.
- `docker-compose.yml` — pass `TAVILY_API_KEY`, `MEGA_RO_PASSWORD` into both `api` and `worker` service `environment:` blocks.
- `backend/tests/` — NEW (or extend) per-tool unit tests covering every I/O Matrix row.

## Tasks & Acceptance

**Execution:**
- [x] `backend/app/models.py` — add `PlannedToolCall` and `RoutingPlan.tool_calls`; default `[]` so existing call sites (e.g. `DEFAULT_FALLBACK_PLAN`) keep working without edits.
- [x] `backend/app/settings.py` — add `TAVILY_API_KEY`, `MEGA_RO_PASSWORD`.
- [x] `backend/app/sql/003_readonly_role.sql` — create `mega_ro` role idempotently with `SELECT`-only privileges.
- [x] `backend/app/bootstrap.py` — run `003_readonly_role.sql` after `001_init.sql`; substitute `MEGA_RO_PASSWORD` safely (use `format()` / quoted literal — never f-string user input).
- [x] `backend/app/db.py` — add `create_ro_pool()` factory.
- [x] `backend/app/worker.py` & `main.py` — open RO pool at startup; teardown on shutdown; expose to `sql_lookup`.
- [x] `backend/app/tools/web_search.py` — implement Tavily call + key-sentinel guard + Tavily-to-README schema mapping (snippet = content[:500], relevance_score = score).
- [x] `backend/app/tools/code_exec.py` — AST banned-import walker + subprocess sandbox per Boundaries.
- [x] `backend/app/tools/sql_lookup.py` — LLM `emit_sql` tool-call + `SELECT`-prefix guard + RO-pool execute with statement timeout.
- [x] `backend/app/tools/registry.py` — `REGISTRY` map + `lookup(name)` raising `KeyError` (caller converts to `MALFORMED`).
- [x] `backend/app/tools/__init__.py` — re-export `registry`.
- [x] `backend/app/agents/orchestrator.py` — extend `ROUTING_PLAN_TOOL` schema and parse `tool_calls`.
- [x] `backend/app/agents/prompts/orchestrator.md` — describe the 4 tools and selection criteria.
- [x] `backend/app/pipeline.py` — dispatch `ctx.routing_plan.tool_calls` keyed by `agent_id` via `run_with_retry` BEFORE the matching agent runs; persist results into `ctx.agent_outputs["tools"]`.
- [x] `backend/requirements.txt` — explicit `httpx` pin.
- [x] `.env.example` — add the two new vars.
- [x] `docker-compose.yml` — pass the two new vars into `api` and `worker`.
- [x] `backend/tests/test_tool_web_search.py` — cover 5 I/O Matrix rows (happy, TIMEOUT, EMPTY, MALFORMED×2). Monkeypatch `httpx.AsyncClient.post`.
- [x] `backend/tests/test_tool_code_exec.py` — cover 5 I/O Matrix rows (happy, TIMEOUT with low timeout, MALFORMED per banned import, EMPTY, EXEC_ERROR).
- [x] `backend/tests/test_tool_sql_lookup.py` — cover 4 I/O Matrix rows (happy, MALFORMED non-SELECT bypassing LLM, TIMEOUT with `statement_timeout='1s'`, EXEC_ERROR on bad column). Use the real `mega_ro` pool via the test docker stack.
- [x] `backend/tests/test_pipeline_tool_dispatch.py` — verify a planted `RoutingPlan.tool_calls=[{agent_id:"rag", tool_name:"web_search", input:{}}]` results in a `tool_calls` row and a `ctx.agent_outputs["tools"]["web_search"]` entry before `rag` runs.

**Acceptance Criteria:**
- Given a forced TIMEOUT/EMPTY/MALFORMED/EXEC_ERROR per tool, when the test harness invokes the tool through `run_with_retry`, then the `tool_calls` row records `tool_name`, `input`, `output`, `latency_ms`, `success`, and the correct `error_code`.
- Given `code_exec` input `"import os\nprint(1)"`, when executed, then no subprocess spawns and `ToolResult.error_code == "MALFORMED"`.
- Given the `sql_lookup` flow, when an LLM emits `DROP TABLE jobs;`, then the SQL is rejected pre-execute with `error_code="MALFORMED"`; **and** even if it reached execute, the `mega_ro` role would refuse write privileges.
- Given `TAVILY_API_KEY` unset, when `web_search` runs, then it returns `MALFORMED` synchronously with no HTTP traffic.
- Given a `RoutingPlan` with `tool_calls=[{agent_id:"rag", tool_name:"web_search", input:{"query":"..."}}]`, when the pipeline runs, then exactly one `tool_calls` row appears with `tool_name='web_search'`, the matching `tool_call_start`/`tool_call_end` SSE pair is emitted, and the result lands in `ctx.agent_outputs["tools"]["web_search"]` BEFORE `rag` starts.
- Given an Orchestrator LLM response that omits `tool_calls`, when parsed, then `RoutingPlan.tool_calls == []` and the pipeline runs identically to pre-E1 behavior (back-compat).

## Spec Change Log

### 2026-05-11 — Patch loop after step-04 review (iteration 1)

Three review agents (blind hunter, edge case hunter, acceptance auditor) ran in parallel. No findings rose to **intent_gap** or **bad_spec** — the frozen intent and the Code Map / Tasks were consistent with the README contract. Eight findings were classified **patch** and fixed in-place; remaining items appended to `deferred-work.md`.

**Patches applied:**
1. `backend/app/bootstrap.py` + `backend/app/sql/003_readonly_role.sql` — removed the `DO $$ ... $$` block that embedded `MEGA_RO_PASSWORD` as a quoted literal. A password containing `$$` could terminate the dollar-quote early and execute arbitrary SQL as superuser. Replaced with Python-side `pg_roles` check + `CREATE ROLE` / `ALTER ROLE` as separate plain SQL statements, where single-quote escape is sufficient.
2. `backend/app/tools/sql_lookup.py` — added `_json_safe()` coercing UUID / datetime / Decimal / bytes (and nested collections) before returning row values. Without this, `run_with_retry`'s `json.dumps(result.data)` would crash on every successful SELECT against tables with UUID PKs and mark the entire job FAILED.
3. `backend/app/worker.py` — also runs `bootstrap.init_schema` on startup. Worker can boot before the API; without this the `mega_ro` role wouldn't exist when the worker's `create_ro_pool()` ran, and `sql_lookup` would stay disabled for the worker's lifetime. Migration is idempotent.
4. `backend/app/agents/orchestrator.py` — `ALLOWED_TOOL_AGENTS` no longer includes `critique`. The pipeline iterates `AGENT_REGISTRY` only; critique is created ad-hoc and never matches the dispatch loop, so planning tool_calls for it would be silently dropped. `ALLOWED_TOOLS` also no longer includes `self_reflection` since the pipeline runs that implicitly.
5. `backend/app/tools/web_search.py` — `error_message` now names the actual env var (`TAVILY_API_KEY`).
6. `backend/app/main.py` + `backend/app/worker.py` — log a warning when `create_ro_pool()` fails (previously silent).
7. `backend/app/tools/code_exec.py` — subprocess now uses `cwd="/tmp"` as defense-in-depth (real isolation is post-extended #3).
8. `backend/app/agents/prompts/orchestrator.md` — trimmed twice from 29 → 15 lines (123% → ~64% chars vs baseline). Further trim would degrade tool-input recall. 30% guideline overshot; logged in `deferred-work.md`.

**KEEP (preserved across the patch loop):**
- Orchestrator-driven dispatch (`pipeline._dispatch_tool_calls` drains tool_calls per-agent before the agent runs).
- `__tool_input__` convention for passing per-call input through `ctx.agent_outputs` (reviewer-flagged as a smell but no concrete failure; preserves the `(ctx, llm) -> ToolResult` signature so `runner.py` stays untouched).
- Synchronous fast-fail for stub Tavily keys.
- Single second-pool model for `mega_ro` rather than per-call connections.
- The full I/O Matrix — drove every fix.

## Design Notes

**Why Orchestrator-driven, not agent-internal:** README §Orchestrator Agent mandates routing decisions live on the Orchestrator. `self_reflection` inside Critique remains an advisory exception; everything new is routed. This keeps the agent layer dumb about tool catalogue and lets E2's retry FSM live entirely in `tools/`.

**`mega_ro` password handling:** the `003_readonly_role.sql` migration must NOT hard-code the password. Read `MEGA_RO_PASSWORD` from `settings` at bootstrap time and apply via parameterized SQL — either by reading the SQL template and substituting a safely-quoted literal (`format_literal`) in Python, or by issuing `CREATE ROLE`/`ALTER ROLE ... PASSWORD` as separate parameterized statements. The migration must be idempotent: detect existing role via `pg_roles` and skip create, but always re-apply grants.

**`sql_lookup` schema injection:** read column names from `information_schema.columns WHERE table_schema='public'` once at pool startup, cache, inject into the `emit_sql` system prompt so the model doesn't hallucinate table/column names. Re-fetch on cache miss only — not per-call.

**`code_exec` banned modules (verbatim from plan):** `{"os","sys","subprocess","socket","urllib","urllib3","requests","httpx","ctypes","pathlib","shutil","builtins","importlib"}`.

**Test concurrency hazard:** `sql_lookup` tests share one `mega_ro` pool. Use `pytest-asyncio` strict mode and per-test transactions that roll back, or each test asserts on row counts using `WHERE called_at >= <test_start_marker>`.

## Verification

**Commands:**
- `docker compose up --build` -- expected: all four containers stay healthy ≥30s.
- `docker compose exec db psql -U mega -d mega -c "SELECT rolname FROM pg_roles WHERE rolname='mega_ro';"` -- expected: one row.
- `docker compose exec db psql -U mega_ro -d mega -c "INSERT INTO jobs(id) VALUES (gen_random_uuid());"` -- expected: ERROR permission denied for table jobs.
- `docker compose exec api pytest backend/tests/test_tool_web_search.py backend/tests/test_tool_code_exec.py backend/tests/test_tool_sql_lookup.py backend/tests/test_pipeline_tool_dispatch.py -v` -- expected: all green.
- `curl -X POST http://localhost:8000/query -d '{"query":"List 3 jobs from the database"}' -H 'content-type: application/json'` then stream — expected: a `sql_lookup` `tool_call_start`/`tool_call_end` event pair appears in the SSE before the `rag` agent_start (when Orchestrator planned it).

**Manual checks:**
- Inspect the orchestrator prompt diff — the new tool descriptions should add ≤30% to the file. If it grows beyond that, trim before merging (E3 budget concerns).

## Suggested Review Order

**Routing schema & dispatch (entry point)**

- The schema gate — what the Orchestrator LLM is allowed to emit.
  [`orchestrator.py:24`](../../backend/app/agents/orchestrator.py#L24)

- Where parsed tool_calls become a typed list, with invalid entries filtered.
  [`orchestrator.py:79`](../../backend/app/agents/orchestrator.py#L79)

- The dispatch hook in the agent loop — tools run before the consumer agent.
  [`pipeline.py:85`](../../backend/app/pipeline.py#L85)

- The dispatcher itself — drains per-agent, runs through `run_with_retry`, persists to `ctx.agent_outputs["tools"]`.
  [`pipeline.py:116`](../../backend/app/pipeline.py#L116)

- The data model — added `PlannedToolCall` and `RoutingPlan.tool_calls` with empty-list default for back-compat.
  [`models.py:83`](../../backend/app/models.py#L83)

**Tool implementations**

- `web_search`: synchronous fast-fail on stub keys (no HTTP), then Tavily → README schema mapping.
  [`web_search.py:36`](../../backend/app/tools/web_search.py#L36)

- `code_exec`: AST-based banned-import walker; subprocess runs with `-I -S`, `env={}`, `cwd="/tmp"`.
  [`code_exec.py:34`](../../backend/app/tools/code_exec.py#L34)

- `sql_lookup`: `_is_select_only` prefix check + `_json_safe` row coercion (UUID / datetime / Decimal / bytes).
  [`sql_lookup.py:89`](../../backend/app/tools/sql_lookup.py#L89)

- Tool registry: name → ToolFn map consumed by the dispatcher.
  [`registry.py:1`](../../backend/app/tools/registry.py#L1)

**Read-only DB role**

- The role create/alter logic, moved OUT of the DO block to defeat `$$`-in-password injection.
  [`bootstrap.py:25`](../../backend/app/bootstrap.py#L25)

- Grants only — idempotent re-application.
  [`003_readonly_role.sql:1`](../../backend/app/sql/003_readonly_role.sql#L1)

- Second asyncpg pool keyed to `mega_ro`.
  [`db.py:30`](../../backend/app/db.py#L30)

**Lifecycle wiring**

- Worker startup runs `init_schema` (defeats worker-before-API race) + opens the RO pool.
  [`worker.py:31`](../../backend/app/worker.py#L31)

- API lifespan opens the RO pool and exposes it to `sql_lookup`.
  [`main.py:36`](../../backend/app/main.py#L36)

- Orchestrator prompt — describes the 3 tools in ≤64% chars vs baseline.
  [`orchestrator.md:1`](../../backend/app/agents/prompts/orchestrator.md#L1)

**Tests**

- Pipeline dispatch test — verifies the AC5 ordering invariant.
  [`test_pipeline_tool_dispatch.py:31`](../../backend/tests/test_pipeline_tool_dispatch.py#L31)

- `web_search` 6 I/O Matrix rows.
  [`test_tool_web_search.py:1`](../../backend/tests/test_tool_web_search.py#L1)

- `code_exec` banned-import parametrize + timeout + EMPTY + EXEC_ERROR.
  [`test_tool_code_exec.py:1`](../../backend/tests/test_tool_code_exec.py#L1)

- `sql_lookup` integration tests (skip if mega_ro unavailable).
  [`test_tool_sql_lookup.py:1`](../../backend/tests/test_tool_sql_lookup.py#L1)

**Config**

- `.env.example`, `docker-compose.yml`, `requirements.txt` (httpx, pytest), `Dockerfile` (tests copied for `docker compose exec api pytest`).
