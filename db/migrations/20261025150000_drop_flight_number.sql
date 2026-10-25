-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- The second half of 20261025140000_service_number: nothing reads flight_number since that deploy.
-- Copy anything the old worker wrote between that migration and its replacement starting, then drop.
-- Deploy only after 20261025140000 has been live, or the old web would still select the column.
UPDATE reservation SET service_number = flight_number
WHERE service_number IS NULL AND flight_number IS NOT NULL;
-- squawk-ignore ban-drop-column
ALTER TABLE reservation DROP COLUMN flight_number;

-- migrate:down
ALTER TABLE reservation ADD COLUMN flight_number text;
UPDATE reservation SET flight_number = service_number;
