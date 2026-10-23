-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- An address that can sign in. app_user rows also exist for addresses that have only ever had
-- mail filed for them; they get an account when someone registers a passkey for them with an
-- invite, and keep their trips.
CREATE TABLE account (
    email text PRIMARY KEY REFERENCES app_user (email),
    -- The WebAuthn user handle: random, so authenticators never store the address.
    webauthn_user_id bytea NOT NULL UNIQUE,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE passkey (
    credential_id bytea PRIMARY KEY,
    email text NOT NULL REFERENCES account (email) ON DELETE CASCADE,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL,
    transports text[] NOT NULL DEFAULT '{}',
    backed_up boolean NOT NULL,
    name text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    last_used_at timestamptz
);
CREATE INDEX passkey_email_idx ON passkey (email);

-- Sign-in sessions. Only a hash of the cookie's token is stored.
CREATE TABLE web_session (
    token_hash bytea PRIMARY KEY,
    email text NOT NULL REFERENCES account (email) ON DELETE CASCADE,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX web_session_email_idx ON web_session (email);

-- One-time links (from `python -m triptych.invite`) that let someone register a passkey for an
-- address. The only way to get a first passkey, and the recovery path for a lost one.
CREATE TABLE invite (
    token_hash bytea PRIMARY KEY,
    email text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);

-- Outstanding WebAuthn challenges, each usable once.
CREATE TABLE webauthn_challenge (
    id uuid PRIMARY KEY,
    challenge bytea NOT NULL,
    purpose text NOT NULL CHECK (purpose IN ('register', 'login')),
    -- Registration only: who the passkey is for, the user handle offered to the authenticator,
    -- and the invite to spend (null when a signed-in user adds another passkey).
    email text,
    webauthn_user_id bytea,
    invite_hash bytea,
    expires_at timestamptz NOT NULL
);

-- migrate:down
DROP TABLE webauthn_challenge;
DROP TABLE invite;
DROP TABLE web_session;
DROP TABLE passkey;
DROP TABLE account;
