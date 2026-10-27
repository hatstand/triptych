-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Who added a reservation by hand, when it wasn't the trip's owner but someone they shared it with
-- to edit (sharing.py). The trip page says "Added by" them.
ALTER TABLE reservation ADD COLUMN added_by uuid REFERENCES app_user (id) ON DELETE SET NULL;

-- migrate:down
ALTER TABLE reservation DROP COLUMN added_by;
