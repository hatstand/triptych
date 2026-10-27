-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Links that show a trip read-only, without anything that could change a booking, to whoever has
-- one (web /s/<token>). Only the token's SHA-256 is kept. Deleting the row revokes the link.
CREATE TABLE share_link (
    id uuid PRIMARY KEY,
    trip_id uuid NOT NULL REFERENCES trip (id) ON DELETE CASCADE,
    token_sha256 bytea NOT NULL UNIQUE,
    label text,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    last_used_at timestamptz
);
CREATE INDEX share_link_trip_idx ON share_link (trip_id);

-- migrate:down
DROP TABLE share_link;
