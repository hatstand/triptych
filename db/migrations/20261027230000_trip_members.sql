-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- People a trip's owner invited, by email address (sharing.py). An invite is pending until someone
-- with that address on their account accepts it, which sets user_id; until then it's matched by
-- address, so it reaches whoever adds the address later. Deleting the row ends their access.
CREATE TABLE trip_member (
    trip_id uuid NOT NULL REFERENCES trip (id) ON DELETE CASCADE,
    email text NOT NULL,
    user_id uuid REFERENCES app_user (id) ON DELETE CASCADE,
    role text NOT NULL CHECK (role IN ('view', 'edit')),
    invited_by uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    accepted_at timestamptz,
    PRIMARY KEY (trip_id, email),
    UNIQUE (trip_id, user_id),
    CHECK ((user_id IS NULL) = (accepted_at IS NULL))
);
CREATE INDEX trip_member_user_idx ON trip_member (user_id);
CREATE INDEX trip_member_email_idx ON trip_member (email) WHERE user_id IS NULL;

-- migrate:down
DROP TABLE trip_member;
