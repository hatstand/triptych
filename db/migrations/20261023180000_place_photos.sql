-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- What to look a trip's photo up by: its destination, plus the country when an airport or station
-- in the trip says which one ("Lisbon, Portugal"). Set by the same code that names the trip; the
-- worker fills it in for trips from before this column on its next start.
ALTER TABLE trip ADD COLUMN IF NOT EXISTS place text;

-- One representative photo per place, from Wikimedia Commons, shared by every trip there. The
-- image is a copy (Wikimedia asks not to hotlink), stored with what its licence needs to credit
-- it. A row without an image means nothing suitable was found, so the search isn't repeated.
CREATE TABLE IF NOT EXISTS place_photo (
    place text PRIMARY KEY,
    image bytea,
    content_type text,
    file text,
    page_url text,
    article_url text,
    artist text,
    licence text,
    licence_url text,
    fetched_at timestamptz NOT NULL DEFAULT now()
);

-- migrate:down
DROP TABLE IF EXISTS place_photo;
-- squawk-ignore ban-drop-column
ALTER TABLE trip DROP COLUMN IF EXISTS place;
