-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Every original input (raw MIME email, uploaded file, pasted text) is kept, so it can be
-- re-extracted with a new model or prompt and derived data can be rebuilt. The bytes live in an R2
-- bucket, encrypted with the owner's own keys (inputs.py); these tables describe them.

-- Each user's Tink keysets (an AEAD for the bytes, a PRF that names the objects), encrypted by the
-- master keyset, which never leaves marvin. Deleting the row makes every object of theirs
-- unreadable, wherever a copy of it is.
CREATE TABLE user_key (
    user_id uuid PRIMARY KEY REFERENCES app_user (id) ON DELETE CASCADE,
    aead_keyset bytea NOT NULL,
    prf_keyset bytea NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- One row per distinct original per user: the same bytes twice are one object. Never deduped
-- across users.
CREATE TABLE raw_input (
    id uuid PRIMARY KEY,
    owner uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    sha256 bytea NOT NULL,
    object_key text NOT NULL,
    size bigint NOT NULL,
    source text NOT NULL,
    content_type text NOT NULL,
    filename text,
    received_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT raw_input_owner_sha256_key UNIQUE (owner, sha256)
);

-- One row per extraction of an ingest: which model and prompt ran, and what came back, so
-- reservations can be re-derived from `output` without the GPU, or compared across models.
CREATE TABLE extraction_run (
    id uuid PRIMARY KEY,
    ingest_id text NOT NULL,
    owner uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    input_id uuid REFERENCES raw_input (id) ON DELETE CASCADE,
    model text NOT NULL,
    model_digest text,
    prompt_version text NOT NULL,
    code_version text NOT NULL,
    params jsonb NOT NULL,
    purpose text NOT NULL,
    output jsonb,
    error text,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT extraction_run_purpose_check CHECK (purpose IN ('ingest', 'reprocess', 'shadow', 'eval'))
);
CREATE INDEX extraction_run_ingest_idx ON extraction_run (ingest_id);
CREATE INDEX extraction_run_owner_idx ON extraction_run (owner);
CREATE INDEX extraction_run_input_idx ON extraction_run (input_id);

ALTER TABLE inbox_item ADD COLUMN input_id uuid, ADD COLUMN current_run_id uuid;
ALTER TABLE reservation ADD COLUMN source_run_id uuid;

-- migrate:down
ALTER TABLE reservation DROP COLUMN source_run_id;
ALTER TABLE inbox_item DROP COLUMN input_id, DROP COLUMN current_run_id;
DROP TABLE extraction_run;
DROP TABLE raw_input;
DROP TABLE user_key;
