-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Whether a trip shared with someone is in their calendar feed (they can take it out).
ALTER TABLE trip_member ADD COLUMN in_feed boolean DEFAULT true NOT NULL;

-- migrate:down
ALTER TABLE trip_member DROP COLUMN in_feed;
