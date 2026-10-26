-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- The ingests whose examples (extraction_example) the model was shown before this run's document,
-- so a run's answer can be traced to what it was taught. Empty while LLM_EXAMPLES is off.
ALTER TABLE extraction_run ADD COLUMN examples text[] NOT NULL DEFAULT '{}';

-- migrate:down
ALTER TABLE extraction_run DROP COLUMN examples;
