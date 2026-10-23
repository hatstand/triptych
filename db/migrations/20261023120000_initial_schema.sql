-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

CREATE TABLE app_user (
    email text PRIMARY KEY,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE trip (
    id uuid PRIMARY KEY,
    owner text NOT NULL REFERENCES app_user (email),
    name text NOT NULL,
    destination text,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE reservation (
    id uuid PRIMARY KEY,
    trip_id uuid NOT NULL REFERENCES trip (id) ON DELETE CASCADE,
    kind text NOT NULL CHECK (kind IN ('flight', 'lodging', 'car', 'rail', 'restaurant', 'activity', 'other')),
    title text NOT NULL,
    provider text,
    confirmation_code text,
    -- Wall-clock time where the segment happens, with the IANA zone kept alongside. This is
    -- deliberately not timestamptz: a 15:00 hotel check-in is 15:00 local whatever the zone
    -- database later says, and the zone may be unknown when we first see it.
    -- squawk-ignore prefer-timestamptz
    start_local timestamp NOT NULL,
    start_tz text,
    -- squawk-ignore prefer-timestamptz
    end_local timestamp,
    end_tz text,
    start_location text,
    end_location text,
    address text,
    city text,
    flight_number text,
    seat text,
    travelers text[] NOT NULL DEFAULT '{}',
    notes text,
    source_ingest_id text,
    -- A re-sent/updated confirmation replaces the earlier copy of the same segment in a trip.
    dedupe_key text NOT NULL,
    CONSTRAINT reservation_trip_dedupe_key UNIQUE (trip_id, dedupe_key)
);

-- New, empty tables: plain CREATE INDEX is fine here. Later migrations on populated tables must
-- use CONCURRENTLY (squawk enforces it).
CREATE INDEX trip_owner_idx ON trip (owner);
CREATE INDEX reservation_trip_start_idx ON reservation (trip_id, start_local);

CREATE TABLE inbox_item (
    ingest_id text PRIMARY KEY,
    owner text NOT NULL REFERENCES app_user (email),
    source text NOT NULL,
    label text NOT NULL,
    received_at timestamptz NOT NULL,
    state text NOT NULL,
    message text,
    trip_ids uuid[] NOT NULL DEFAULT '{}'
);
CREATE INDEX inbox_item_owner_received_idx ON inbox_item (owner, received_at DESC);

CREATE TABLE notification (
    id uuid PRIMARY KEY,
    owner text NOT NULL REFERENCES app_user (email),
    at timestamptz NOT NULL,
    trip_id uuid REFERENCES trip (id) ON DELETE SET NULL,
    text text NOT NULL
);
CREATE INDEX notification_owner_at_idx ON notification (owner, at DESC);

-- migrate:down
DROP TABLE notification;
DROP TABLE inbox_item;
DROP TABLE reservation;
DROP TABLE trip;
DROP TABLE app_user;
