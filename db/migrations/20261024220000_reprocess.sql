-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- One row per Reprocess workflow (services/reprocess.py): an ingest extracted again with another
-- model, or re-derived from a stored run's output, and what that did, or would do, to its
-- reservations. Admin starts them in batches; shadow runs come from Ingest when SHADOW_MODEL is set.
CREATE TABLE reprocess (
    id text PRIMARY KEY,
    batch_id text NOT NULL,
    ingest_id text NOT NULL,
    owner uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    mode text NOT NULL,
    model text,
    from_run_id uuid,
    run_id uuid,
    state text NOT NULL DEFAULT 'queued',
    diff jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    finished_at timestamptz,
    CONSTRAINT reprocess_mode_check CHECK (mode IN ('dry_run', 'apply', 'shadow')),
    CONSTRAINT reprocess_state_check CHECK (state IN ('queued', 'done', 'failed'))
);
CREATE INDEX reprocess_batch_idx ON reprocess (batch_id);
CREATE INDEX reprocess_ingest_idx ON reprocess (ingest_id);

-- migrate:down
DROP TABLE reprocess;
