-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- When its owner said a "no travel" ingest is a booking after all (the inbox's "This is a booking"):
-- it was then read again as one. A correction like an edit, for the eval set and worked examples.
ALTER TABLE inbox_item ADD COLUMN reclassified_at timestamptz;

-- migrate:down
ALTER TABLE inbox_item DROP COLUMN reclassified_at;
