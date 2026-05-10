---
title: 'Slice 3.5-4.5: Static frontend (index.html + app.js + styles.css) + /trace'
type: 'feature'
created: '2026-05-10'
status: 'in-progress'
baseline_commit: 'NO_COMMITS'
context:
  - '{project-root}/_bmad-output/planning-artifacts/plan.md'
  - '{project-root}/_bmad-output/implementation-artifacts/spec-slice-2-5-3-5-critique.md'
---

<frozen-after-approval reason="human-owned intent — do not modify unless human renegotiates">

## Intent

**Problem:** The pipeline streams events but a human can't watch them. There's no way to demo the system without raw `curl` traces. The eval harness, demo recording, and submission all need a browser-visible UI.

**Approach:** Add a single-page static frontend served by FastAPI from `app/static/`. Three files: `index.html` (markup), `app.js` (EventSource consumer + DOM dispatch), `styles.css` (dark, system-font, CSS-variable palette). Add a `GET /trace/{job_id}` endpoint so the page can fetch the canonical final answer when streaming ends.

## Boundaries & Constraints

**Always:**
- Static assets at exactly `backend/app/static/{index.html,app.js,styles.css}`. No subdirectories, no build artifacts.
- `main.py`: mount `StaticFiles(directory="app/static")` at `/static`; add `GET /` returning `FileResponse("app/static/index.html")`.
- `GET /trace/{job_id}` returns `{job_id, status, final_answer, routing_plan}` from the `jobs` table. 404 if not found.
- Existing endpoint contracts are unchanged: `GET /healthz`, `POST /query`, `GET /stream/{job_id}`.
- Layout: top bar (textarea + Submit + status) above a flex row of left rail (30%) "Agent Activity" timeline and main (70%) "Live Answer" panel.
- Live Answer panel during streaming renders ONLY `token` events with `agent_id === "synthesis"`. RAG draft tokens are intentionally suppressed (canonical UX choice from plan.md).
- On `job_complete`: close EventSource, `GET /trace/{job_id}`, replace Live Answer with the canonical `final_answer` rendered sentence-by-sentence with citation badges.
- Citation badge: `<span class="citation">[chunk_id]</span>` rendered AFTER the sentence text. Strip any `[c1,c2]` prefix already in `sentence_text`; use `source_chunk_ids` as the source of truth for badge content.
- Color palette via CSS variables: `--c-orchestrator: blue, --c-decomposition: purple, --c-rag: green, --c-synthesis: orange, --c-critique: red, --c-tools: gray`. Each timeline entry tagged by class.
- Submit button is disabled from POST until `job_complete` or `error` arrives.

**Ask First:**
- Adding chunk-text drill-down on citation click (out of scope for this slice).
- Persisting in-flight job state across page reloads (out of scope per locked decision #10).
- Adding any client-side library, framework, or build step.

**Never:**
- No npm/yarn/Vite/webpack/React/etc. Pure HTML + ES2022 vanilla JS + CSS.
- No CDN <script> tags. No external font loads. System fonts only.
- No alteration of `docker-compose.yml` or `Dockerfile`. The existing `COPY app ./app` already pulls the static folder.
- No re-rendering of the Live Answer between `job_complete` and the `/trace` fetch — keep the streamed synthesis text visible until replaced.
- **Cut policy:** if elapsed slice time exceeds hour 4.5, drop the timeline panel entirely. Keep query input + Live Answer panel only. Configure via `body[data-cut-timeline="true"]` flag in HTML; CSS hides the panel and expands the answer to 100% width.

## I/O & Edge-Case Matrix

| Scenario | Input / State | Expected Output / Behavior | Error Handling |
|---|---|---|---|
| Submit happy path | User types query, clicks Submit | POST /query → 202; EventSource opens; events render to timeline; synthesis tokens accumulate in Live Answer; on job_complete, /trace replaces panel with canonical SentenceProvenance + citation badges | n/a |
| Empty query | Textarea empty | Submit disabled until non-empty (client-side check) | n/a |
| /query 4xx/5xx | Backend rejects (e.g. BUDGET_EXCEEDED) | Error banner shows error_code + message; Submit re-enables | parse JSON error, fallback to status text |
| SSE error event | error event arrives mid-stream | Red error banner above timeline; EventSource stays open until job_complete; Submit re-enabled | display only |
| Non-synthesis token | rag/decomposition emits token events | Ignored by Live Answer panel — only timeline shows the agent_start/end | n/a |
| /trace 404 on job_complete | Race / job purged | Keep streamed synthesis text as final; show subtle "trace unavailable" footnote | log to console, don't block UI |
| Page reload mid-job | User refreshes during stream | Page resets to idle; previous job continues in worker but is invisible to UI; user can resubmit | per locked decision #10 |
| Timeline cut | `body[data-cut-timeline="true"]` | Left rail hidden, Live Answer is 100% width | CSS-only, no JS branch |

</frozen-after-approval>

## Code Map

- `backend/app/main.py` -- mount `/static`, add `GET /`, add `GET /trace/{job_id}`
- `backend/app/static/index.html` -- single-page markup with top bar, timeline, answer panel; loads `app.js` deferred and `styles.css`
- `backend/app/static/app.js` -- ES2022 module-free script; one `submit` handler, one `EventSource.onmessage` dispatch; renderers for timeline entry and citation-bearing sentences
- `backend/app/static/styles.css` -- CSS variables, dark theme, three-row layout (header, query bar, main flex row), <200 lines

## Tasks & Acceptance

**Execution:**
- [x] `backend/app/main.py` -- StaticFiles mount, `/`, `/trace/{job_id}` (asyncpg fetchrow)
- [x] `backend/app/static/index.html` -- markup
- [x] `backend/app/static/styles.css` -- dark theme, palette vars, cut-timeline rule
- [x] `backend/app/static/app.js` -- POST /query, EventSource open/close, dispatch, /trace replace, citation badge render

**Acceptance Criteria:**
- Given the stack is up, when `curl http://localhost:8000/`, then HTTP 200 and the body contains `<title>Mega AI</title>` and a `<script` tag pointing at `/static/app.js`.
- Given the stack is up, when `curl http://localhost:8000/static/app.js` and `/static/styles.css`, then both return HTTP 200 with non-empty bodies.
- Given a completed run, when `curl http://localhost:8000/trace/<job_id>`, then HTTP 200 with JSON containing `job_id`, `status`, `final_answer`, `routing_plan`.
- Given an unknown UUID, when `curl http://localhost:8000/trace/<bad-uuid>`, then HTTP 404.
- Given a real browser at `http://localhost:8000`, when the user submits "What is the retry policy?", then ≥10 timeline entries render (orchestrator + decomposition + rag + synthesis + critique:* + self_reflection tool_calls + job_complete), the Live Answer panel fills with synthesis tokens, and on completion the panel re-renders with at least one citation badge such as `[c1-retry]`. (User-verified.)
- Given the run, when the browser DevTools console is checked, then there are no errors and no 404s on static assets. (User-verified.)

## Spec Change Log

## Design Notes

**Why suppress non-synthesis tokens in Live Answer:** showing every agent's stream of thought clutters the panel and obscures the final, citation-grounded answer. The timeline already shows that those agents ran. RAG's draft is functionally a private working pass; synthesis is the canonical user-facing voice.

**Why /trace replace after job_complete:** during streaming, the Live Answer is a token concatenation that may contain `[c1,c2]` prefix syntax, half-formed sentences, or citation list trailing commas. The canonical `final_answer` is structured `SentenceProvenance` JSON — re-rendering from it gives clean sentences with proper badges. The visual swap is intentional and signals "answer is final".

**Sentence-prefix stripping:** Synthesis output has lines like `[c1-retry] Tools support up to two retries.` The parser already extracted `c1-retry` into `source_chunk_ids`, so the `[c1-retry] ` prefix in `sentence_text` is duplicate. Strip via regex `^\s*\[[^\]]*\]\s*` before rendering. Badges come from the structured field, never from re-parsing the text.

**Timeline entry shape:**
```html
<li class="entry agent-rag">
  <span class="dot"></span>
  <span class="agent">rag</span>
  <span class="event">AGENT_END</span>
  <span class="lat">2143ms</span>
</li>
```

## Verification

**Commands:**
- `docker compose build api && docker compose up -d --force-recreate api` -- expected: api rebuilds and starts
- `curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8000/` -- expected: 200
- `curl -s http://localhost:8000/static/app.js | head -1` -- expected: non-empty JS line
- `curl -s http://localhost:8000/trace/$(curl -s http://localhost:8000/trace/00000000-0000-0000-0000-000000000000 -o /dev/null -w '%{http_code}')` -- not for AC; just confirm 404 path works
- Manual browser load + submit query -- user-verified per AC
