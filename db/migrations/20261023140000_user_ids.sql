-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Users get a surrogate id; email addresses become one kind of identifier pointing at it, so a
-- user can have several (and later SSO identities). Everything a user owns moves to the id.
--
-- This rewrites the small app_user, trip, inbox_item and notification tables under ACCESS
-- EXCLUSIVE locks, which squawk rightly flags in general. At this deployment's size (one user,
-- a handful of rows) the rewrite takes milliseconds, and an expand/backfill/contract dance
-- across several deploys would buy nothing, hence the squawk-ignores below.

CREATE TABLE user_email (
    email text PRIMARY KEY,
    user_id uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX user_email_user_idx ON user_email (user_id);

ALTER TABLE app_user ADD COLUMN id uuid;
UPDATE app_user SET id = gen_random_uuid();
INSERT INTO user_email (email, user_id, created_at) SELECT email, id, created_at FROM app_user;

-- squawk-ignore ban-drop-constraint
ALTER TABLE trip DROP CONSTRAINT trip_owner_fkey;
-- squawk-ignore ban-drop-constraint
ALTER TABLE inbox_item DROP CONSTRAINT inbox_item_owner_fkey;
-- squawk-ignore ban-drop-constraint
ALTER TABLE notification DROP CONSTRAINT notification_owner_fkey;

UPDATE trip t SET owner = u.id::text FROM app_user u WHERE t.owner = u.email;
UPDATE inbox_item i SET owner = u.id::text FROM app_user u WHERE i.owner = u.email;
UPDATE notification n SET owner = u.id::text FROM app_user u WHERE n.owner = u.email;

-- squawk-ignore changing-column-type
ALTER TABLE trip ALTER COLUMN owner TYPE uuid USING owner::uuid;
-- squawk-ignore changing-column-type
ALTER TABLE inbox_item ALTER COLUMN owner TYPE uuid USING owner::uuid;
-- squawk-ignore changing-column-type
ALTER TABLE notification ALTER COLUMN owner TYPE uuid USING owner::uuid;

-- squawk-ignore ban-drop-constraint
ALTER TABLE app_user DROP CONSTRAINT app_user_pkey;
-- squawk-ignore ban-drop-column
ALTER TABLE app_user DROP COLUMN email;
-- squawk-ignore adding-not-nullable-field
ALTER TABLE app_user ALTER COLUMN id SET NOT NULL;
ALTER TABLE app_user ALTER COLUMN id SET DEFAULT gen_random_uuid();
-- squawk-ignore constraint-missing-not-valid, adding-serial-primary-key-field
ALTER TABLE app_user ADD CONSTRAINT app_user_pkey PRIMARY KEY (id);

-- squawk-ignore adding-foreign-key-constraint
ALTER TABLE user_email ADD CONSTRAINT user_email_user_id_fkey
    FOREIGN KEY (user_id) REFERENCES app_user (id) ON DELETE CASCADE;
-- squawk-ignore adding-foreign-key-constraint, constraint-missing-not-valid
ALTER TABLE trip ADD CONSTRAINT trip_owner_fkey FOREIGN KEY (owner) REFERENCES app_user (id);
-- squawk-ignore adding-foreign-key-constraint, constraint-missing-not-valid
ALTER TABLE inbox_item ADD CONSTRAINT inbox_item_owner_fkey FOREIGN KEY (owner) REFERENCES app_user (id);
-- squawk-ignore adding-foreign-key-constraint, constraint-missing-not-valid
ALTER TABLE notification ADD CONSTRAINT notification_owner_fkey FOREIGN KEY (owner) REFERENCES app_user (id);

-- migrate:down
-- Not reversible without losing secondary addresses; restore from backup instead.
SELECT 1;
