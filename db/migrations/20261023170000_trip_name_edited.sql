-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Trip names are generated from their reservations and regenerated as more are filed, until the
-- owner renames the trip. Existing trips count as renamed when their name isn't what the old
-- generator ("<destination>, <Month YYYY>") would have produced.
ALTER TABLE trip ADD COLUMN IF NOT EXISTS name_edited boolean NOT NULL DEFAULT false;
UPDATE trip t SET name_edited = true
WHERE t.name <> (
    SELECT coalesce(t.destination, 'Trip') || ', ' || to_char(min(r.start_local), 'FMMonth YYYY')
    FROM reservation r WHERE r.trip_id = t.id
);

-- migrate:down
-- squawk-ignore ban-drop-column
ALTER TABLE trip DROP COLUMN IF EXISTS name_edited;
