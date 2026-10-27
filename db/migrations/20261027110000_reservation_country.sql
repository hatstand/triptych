-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- The ISO 3166-1 alpha-2 code of the country `city` is in, as extraction gave it (or an airport or
-- station's, for transport). Null where unknown, and once the owner corrects the city. Rows filed
-- before this are filled in from their extraction runs when the worker starts.
ALTER TABLE reservation ADD COLUMN IF NOT EXISTS country_code text;

-- migrate:down
-- squawk-ignore ban-drop-column
ALTER TABLE reservation DROP COLUMN IF EXISTS country_code;
