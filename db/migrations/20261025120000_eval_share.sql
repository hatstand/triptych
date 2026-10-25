-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Accounts that share their originals with the extraction eval set (evalset.py). While a row is
-- here, every kept original of theirs is copied, decrypted, to the private eval bucket: all past
-- ones when they opt in, and each new one as it arrives. Deleting the row stops new copies.
CREATE TABLE eval_share (
    user_id uuid PRIMARY KEY REFERENCES app_user (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now()
);

-- Which ingests are in the eval bucket (inputs/<ingest id>/), so sharing again copies only new ones.
CREATE TABLE eval_copy (
    ingest_id text PRIMARY KEY,
    owner uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    copied_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX eval_copy_owner_idx ON eval_copy (owner);

-- migrate:down
DROP TABLE eval_copy;
DROP TABLE eval_share;
