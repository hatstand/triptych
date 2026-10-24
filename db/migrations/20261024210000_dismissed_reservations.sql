-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- What a user deleted, so filing the same ingest again (a Restate retry, or later a reprocess
-- with a new model) doesn't bring it back. One row per deleted reservation, holding the keys a
-- re-extraction of the same document is matched on (db.dismissed). Deleting a trip dismisses
-- every reservation in it. A new email about the same booking is a new ingest, so it is filed.
CREATE TABLE dismissed_reservation (
    owner uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    ingest_id text NOT NULL,
    dedupe_key text NOT NULL,
    kind text NOT NULL,
    confirmation_code text,
    start_date date NOT NULL,
    city text,
    dismissed_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT dismissed_reservation_pkey PRIMARY KEY (owner, ingest_id, dedupe_key)
);

-- migrate:down
DROP TABLE dismissed_reservation;
