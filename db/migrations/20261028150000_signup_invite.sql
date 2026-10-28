-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- A signup started from an admin invite link: the invite's token hash, spent with the signup. It lets
-- the signup in when SIGNUPS is closed, past the waitlist, and into an existing account (recovery).
ALTER TABLE signup ADD COLUMN invite_hash bytea;

-- migrate:down
ALTER TABLE signup DROP COLUMN invite_hash;
