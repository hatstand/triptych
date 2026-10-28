-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Since when an account made by signing up has been waiting for an admin to let it in (SIGNUPS=
-- waitlist, signup.py). Null for everyone else.
ALTER TABLE app_user ADD COLUMN waitlisted_at timestamp with time zone;

-- migrate:down
ALTER TABLE app_user DROP COLUMN waitlisted_at;
