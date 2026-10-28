-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- When mail authenticated as coming from the address proved its owner controls it. New addresses
-- named on an admin invite start unproven; their first DKIM-authenticated mail to plans@ proves them.
ALTER TABLE user_email ADD COLUMN verified_at timestamp with time zone;

-- Everyone here before this was invited by hand, so their addresses count as proven.
UPDATE user_email SET verified_at = created_at;

-- migrate:down
ALTER TABLE user_email DROP COLUMN verified_at;
