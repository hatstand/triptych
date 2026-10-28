-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- When mail authenticated as coming from the address proved its owner controls it. Addresses named
-- on an admin invite start unproven; their first DKIM-authenticated mail to plans@ proves them.
ALTER TABLE user_email ADD COLUMN verified_at timestamp with time zone;

-- migrate:down
ALTER TABLE user_email DROP COLUMN verified_at;
