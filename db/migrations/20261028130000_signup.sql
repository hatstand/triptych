-- migrate:up
SET lock_timeout = '5s';
SET statement_timeout = '60s';

-- Someone signing up, before any account exists (signup.py). The browser holds a token; the code
-- they mail to plans@ is derived from it. Both are stored only as SHA-256. Rows live 30 minutes.
CREATE TABLE signup (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    token_hash bytea NOT NULL UNIQUE,
    code_hash bytea NOT NULL UNIQUE,
    email text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL,
    verified_at timestamp with time zone
);

CREATE INDEX signup_email_idx ON signup (email);

-- migrate:down
DROP TABLE signup;
