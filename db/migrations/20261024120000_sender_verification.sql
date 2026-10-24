-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Mail is filed under the user whose verified sender address it comes from (user_email), and is
-- refused from any other address. A user adds an address on the account page and gets a code;
-- sending mail from that address to plans@ with the code in it proves control of the address and
-- links it to them. Only a hash of the code is stored.
CREATE TABLE sender_verification (
    code_hash bytea PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES app_user (id) ON DELETE CASCADE,
    email text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL
);
CREATE INDEX sender_verification_user_idx ON sender_verification (user_id);

-- migrate:down
DROP TABLE sender_verification;
