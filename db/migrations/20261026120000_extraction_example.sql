-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Worked examples for extraction (examples.py): an ingest whose reservations its owner corrected on
-- the trip page, with the answer they would have wanted. Only ever shown to the model for that same
-- owner's later mail. `changes` says what they corrected, `partial` that some of it couldn't be
-- checked against the text and was left as the model had it. Rewritten on every correction;
-- live_at, ten minutes after the latest one, is when it may first be used, so a correction still
-- being made isn't taught half done.
CREATE TABLE extraction_example (
    ingest_id text PRIMARY KEY REFERENCES inbox_item (ingest_id) ON DELETE CASCADE,
    owner uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    sender_domain text,
    subject text,
    text text NOT NULL,
    answer jsonb NOT NULL,
    changes text[] NOT NULL DEFAULT '{}',
    partial boolean NOT NULL DEFAULT false,
    live_at timestamptz NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX extraction_example_owner_idx ON extraction_example (owner, sender_domain);

-- migrate:down
DROP TABLE extraction_example;
