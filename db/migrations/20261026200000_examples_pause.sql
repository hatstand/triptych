-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Worked examples switched off without a redeploy (examples.py): by the nightly eval when they
-- made extraction worse, or by hand on /admin. At most one row; none means LLM_EXAMPLES decides.
CREATE TABLE examples_pause (
    singleton boolean PRIMARY KEY DEFAULT true CHECK (singleton),
    paused_at timestamptz NOT NULL DEFAULT now(),
    paused_by text NOT NULL,
    reason text NOT NULL
);

-- migrate:down
DROP TABLE examples_pause;
