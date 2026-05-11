# Deferred Work

Findings surfaced during reviews that are out of scope for the current slice
but should be tracked for future attention.

## From E1 review (2026-05-11)

### Tool sandbox depth
- **code_exec sandbox escape via `__import__("os")` / dunder traversal.** The AST walker only blocks static `import` / `from import` nodes; dynamic imports, `__import__`, and class-MRO traversal still load banned modules from the stdlib. README §Known Limitations should call this out; real isolation is post-extended #3 (gVisor / Firecracker). Mitigations in place today: subprocess runs in a Linux worker container, no network egress declared, `cwd=/tmp`, `env={}`, `-I -S`.
- **code_exec / web_search output size caps.** No upper bound on stdout / response body; a huge payload buffers in memory and may exceed Postgres jsonb size limits when persisted by `run_with_retry`. Add a size cap (e.g. 256 KB) in a follow-up.

### sql_lookup hardening
- **LIMIT not enforced.** Prompt asks for `LIMIT <= 100`; check does not verify it. An LLM that omits LIMIT or uses `LIMIT 1000000` is executed (RO role + statement_timeout still cap blast radius).
- **String-literal false-positive on `_is_select_only`.** `SELECT 'INSERT' as x` is rejected because `\bINSERT\b` matches inside the string literal. Defense-in-depth via mega_ro role is fine; the SELECT-check is conservative. Replace with a real SQL parser when convenient.
- **`_schema_cache` TTL / invalidation.** Cache survives the worker process lifetime. New tables added at runtime (E4 FAISS rebuild flows, E8 migrations) won't appear until restart.
- **`ALTER DEFAULT PRIVILEGES` is role-bound.** Tables created by a role other than the bootstrap superuser won't grant SELECT to `mega_ro` automatically. Fine while `init_schema` is the only creator.

### Runner persistence
- ~~`tool_calls.input` stores `{job_id, query_hash}`, not the actual planned input.~~ **Resolved in E2** — pipeline now passes `input_payload` through `run_with_retry`.
- **Empty Tavily results trigger retries.** `_accept(result)` in `runner.py` (now `retry.py`) treats `data=[]` as not-accepted, so an EMPTY web_search burns 3 Tavily calls. Could address by treating `data=[]` as accepted when error_code == EMPTY; needs care so retries still happen on transient empty results vs persistent empties.

## From E2 review (2026-05-11)

### Operational hardening
- **Duplicate `self_reflection` invocation on web_search TIMEOUT fallback.** Pipeline already runs self_reflection at the end of the agent loop; the web_search fallback ALSO runs it via the same `run_with_retry`, doubling LLM cost. Could check `("self_reflection","done")` on ctx and skip the second run, or defer to E3 budget gating.
- **Schema cache survives pool-identity change.** `_schema_cache` / `_compact_schema_cache` are only cleared on `set_ro_pool(None)`. If a future call replaces the pool with a new object (same DB, same role) the cache stays stale. Reset on every `set_ro_pool(...)` if the pool identity changes, or add a TTL.
- **Cosmetic dead field: `code_exec_failed.suggested_replan`** is set to True by the fallback but never read (decomposition uses a truthy check on the dict itself). Either consume the field meaningfully (e.g. only prefix when True) or drop it.

### Infra hygiene
- **Dockerfile bakes `tests/` and `pytest.ini` into the production image.** Acceptable for the assessment; split into a separate test stage / target before prod hardening.
- **Orchestrator prompt grew ~64% in chars** (baseline 692 → 1134). Above the 30% soft guideline noted in the spec's Design Notes. Trimmed twice during patch; further trim only at the cost of model recall on tool inputs. Re-evaluate when E3 budget pressure arrives.

### Speculative / rejected (recorded for context, not actionable)
- `SET LOCAL statement_timeout = {int}` interpolation in `sql_lookup._execute`: safe today because the setting is `int`-typed. Reject unless type loosens.
- `_ro_dsn` lowercases hostnames: only matters if `DATABASE_URL`'s host is case-sensitive (very unusual). Reject.
- `FakeConnection` in tests lacks a `transaction()` async-context: only matters if a future test runs `_execute` through the fake pool.
