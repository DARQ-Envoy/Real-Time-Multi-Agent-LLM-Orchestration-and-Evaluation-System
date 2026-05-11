-- E3: lossless sidecars for context compression. Created before the mega_ro
-- role + grants step in bootstrap, so the role's SELECT-on-all-tables grant
-- covers this table too.

CREATE TABLE IF NOT EXISTS compressed_sidecars (
    id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    job_id          UUID REFERENCES jobs(id),
    field_hash      TEXT NOT NULL,
    field_kind      TEXT NOT NULL,
    content         JSONB NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_sidecars_hash ON compressed_sidecars(field_hash);
