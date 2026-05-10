CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS jobs (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    query           TEXT NOT NULL,
    status          TEXT NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now(),
    completed_at    TIMESTAMPTZ,
    final_answer    JSONB,
    routing_plan    JSONB
);

CREATE TABLE IF NOT EXISTS agent_logs (
    id                  BIGSERIAL PRIMARY KEY,
    job_id              UUID REFERENCES jobs(id),
    timestamp           TIMESTAMPTZ DEFAULT now(),
    agent_id            TEXT NOT NULL,
    event_type          TEXT NOT NULL,
    input_hash          TEXT,
    output_hash         TEXT,
    latency_ms          FLOAT,
    token_count         INT,
    policy_violations   TEXT
);

CREATE TABLE IF NOT EXISTS tool_calls (
    id              BIGSERIAL PRIMARY KEY,
    job_id          UUID REFERENCES jobs(id),
    tool_name       TEXT NOT NULL,
    input           JSONB,
    output          JSONB,
    latency_ms      FLOAT,
    success         BOOL,
    error_code      TEXT,
    accepted        BOOL,
    retry_number    INT DEFAULT 0,
    called_at       TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS eval_runs (
    id          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    run_at      TIMESTAMPTZ DEFAULT now(),
    run_hash    TEXT NOT NULL,
    summary     JSONB
);

CREATE TABLE IF NOT EXISTS eval_cases (
    id              BIGSERIAL PRIMARY KEY,
    run_id          UUID REFERENCES eval_runs(id),
    category        TEXT NOT NULL,
    query           TEXT NOT NULL,
    agent_prompts   JSONB,
    tool_calls      JSONB,
    agent_outputs   JSONB,
    scores          JSONB,
    passed          BOOL
);

CREATE TABLE IF NOT EXISTS prompt_rewrites (
    id               UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    target_prompt_id TEXT NOT NULL,
    original_text    TEXT NOT NULL,
    proposed_text    TEXT NOT NULL,
    unified_diff     TEXT NOT NULL,
    justification    TEXT NOT NULL,
    proposed_at      TIMESTAMPTZ DEFAULT now(),
    status           TEXT DEFAULT 'PENDING',
    decided_at       TIMESTAMPTZ,
    decided_by       TEXT,
    reject_reason    TEXT
);

CREATE TABLE IF NOT EXISTS performance_deltas (
    id              BIGSERIAL PRIMARY KEY,
    rewrite_id      UUID REFERENCES prompt_rewrites(id),
    case_ids        UUID[],
    before_scores   JSONB,
    after_scores    JSONB,
    delta_scores    JSONB,
    rerun_at        TIMESTAMPTZ DEFAULT now()
);
