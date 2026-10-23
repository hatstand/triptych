-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Sign-in is by passkey only. A user's WebAuthn user handle is the 16 bytes of app_user.id: random,
-- stable, and never an email address.
CREATE TABLE passkey (
    credential_id bytea PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL,
    transports text[] NOT NULL DEFAULT '{}',
    backed_up boolean NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);
CREATE INDEX passkey_user_idx ON passkey (user_id);

-- Sign-in sessions. Only a hash of the cookie's token is stored.
CREATE TABLE web_session (
    token_hash bytea PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX web_session_user_idx ON web_session (user_id);

-- One-time links (from `python -m triptych.invite`) that let someone register a passkey for the
-- user an address belongs to (creating the user if the address is new). The only way to get a
-- first passkey, and the recovery path for a lost one.
CREATE TABLE invite (
    token_hash bytea PRIMARY KEY,
    email text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

-- migrate:down
DROP TABLE invite;
DROP TABLE web_session;
DROP TABLE passkey;
