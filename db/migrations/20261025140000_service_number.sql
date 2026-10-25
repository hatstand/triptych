-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Trains have numbers too (G324, Alfa Pendular 121), so flight_number becomes service_number.
-- Added and copied rather than renamed, because the old web keeps reading flight_number until the
-- new one is healthy (deploy/README.md, zero-downtime deploys). Nothing writes flight_number any
-- more; a later migration drops it.
ALTER TABLE reservation ADD COLUMN service_number text;
UPDATE reservation SET service_number = flight_number;

-- migrate:down
ALTER TABLE reservation DROP COLUMN service_number;
