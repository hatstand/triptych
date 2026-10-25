-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- The fields of a reservation its owner has corrected on the trip page. Filing the same segment
-- again, refilling timezones and reprocessing all leave these as the owner set them. The
-- reservation's dedupe_key stays the one its extraction had, so the same booking sent again (or
-- extracted again) still finds it.
ALTER TABLE reservation ADD COLUMN IF NOT EXISTS edited_fields text[] NOT NULL DEFAULT '{}';

-- migrate:down
-- squawk-ignore ban-drop-column
ALTER TABLE reservation DROP COLUMN IF EXISTS edited_fields;
