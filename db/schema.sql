\restrict dbmate

-- Dumped from database version 18.4
-- Dumped by pg_dump version 18.6

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET transaction_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: account; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.account (
    email text NOT NULL,
    webauthn_user_id bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: app_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_user (
    email text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: inbox_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inbox_item (
    ingest_id text NOT NULL,
    owner text NOT NULL,
    source text NOT NULL,
    label text NOT NULL,
    received_at timestamp with time zone NOT NULL,
    state text NOT NULL,
    message text,
    trip_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL
);


--
-- Name: invite; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.invite (
    token_hash bytea NOT NULL,
    email text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: notification; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification (
    id uuid NOT NULL,
    owner text NOT NULL,
    at timestamp with time zone NOT NULL,
    trip_id uuid,
    text text NOT NULL
);


--
-- Name: passkey; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.passkey (
    credential_id bytea NOT NULL,
    email text NOT NULL,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL,
    transports text[] DEFAULT '{}'::text[] NOT NULL,
    backed_up boolean NOT NULL,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone
);


--
-- Name: reservation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reservation (
    id uuid NOT NULL,
    trip_id uuid NOT NULL,
    kind text NOT NULL,
    title text NOT NULL,
    provider text,
    confirmation_code text,
    start_local timestamp without time zone NOT NULL,
    start_tz text,
    end_local timestamp without time zone,
    end_tz text,
    start_location text,
    end_location text,
    address text,
    city text,
    flight_number text,
    seat text,
    travelers text[] DEFAULT '{}'::text[] NOT NULL,
    notes text,
    source_ingest_id text,
    dedupe_key text NOT NULL,
    CONSTRAINT reservation_kind_check CHECK ((kind = ANY (ARRAY['flight'::text, 'lodging'::text, 'car'::text, 'rail'::text, 'restaurant'::text, 'activity'::text, 'other'::text])))
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying NOT NULL
);


--
-- Name: trip; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trip (
    id uuid NOT NULL,
    owner text NOT NULL,
    name text NOT NULL,
    destination text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: web_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.web_session (
    token_hash bytea NOT NULL,
    email text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: webauthn_challenge; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.webauthn_challenge (
    id uuid NOT NULL,
    challenge bytea NOT NULL,
    purpose text NOT NULL,
    email text,
    webauthn_user_id bytea,
    invite_hash bytea,
    expires_at timestamp with time zone NOT NULL,
    CONSTRAINT webauthn_challenge_purpose_check CHECK ((purpose = ANY (ARRAY['register'::text, 'login'::text])))
);


--
-- Name: account account_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_pkey PRIMARY KEY (email);


--
-- Name: account account_webauthn_user_id_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_webauthn_user_id_key UNIQUE (webauthn_user_id);


--
-- Name: app_user app_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_pkey PRIMARY KEY (email);


--
-- Name: inbox_item inbox_item_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbox_item
    ADD CONSTRAINT inbox_item_pkey PRIMARY KEY (ingest_id);


--
-- Name: invite invite_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.invite
    ADD CONSTRAINT invite_pkey PRIMARY KEY (token_hash);


--
-- Name: notification notification_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_pkey PRIMARY KEY (id);


--
-- Name: passkey passkey_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.passkey
    ADD CONSTRAINT passkey_pkey PRIMARY KEY (credential_id);


--
-- Name: reservation reservation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reservation
    ADD CONSTRAINT reservation_pkey PRIMARY KEY (id);


--
-- Name: reservation reservation_trip_dedupe_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reservation
    ADD CONSTRAINT reservation_trip_dedupe_key UNIQUE (trip_id, dedupe_key);


--
-- Name: schema_migrations schema_migrations_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.schema_migrations
    ADD CONSTRAINT schema_migrations_pkey PRIMARY KEY (version);


--
-- Name: trip trip_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trip
    ADD CONSTRAINT trip_pkey PRIMARY KEY (id);


--
-- Name: web_session web_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_session
    ADD CONSTRAINT web_session_pkey PRIMARY KEY (token_hash);


--
-- Name: webauthn_challenge webauthn_challenge_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.webauthn_challenge
    ADD CONSTRAINT webauthn_challenge_pkey PRIMARY KEY (id);


--
-- Name: inbox_item_owner_received_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX inbox_item_owner_received_idx ON public.inbox_item USING btree (owner, received_at DESC);


--
-- Name: notification_owner_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_owner_at_idx ON public.notification USING btree (owner, at DESC);


--
-- Name: passkey_email_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX passkey_email_idx ON public.passkey USING btree (email);


--
-- Name: reservation_trip_start_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX reservation_trip_start_idx ON public.reservation USING btree (trip_id, start_local);


--
-- Name: trip_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX trip_owner_idx ON public.trip USING btree (owner);


--
-- Name: web_session_email_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX web_session_email_idx ON public.web_session USING btree (email);


--
-- Name: account account_email_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.account
    ADD CONSTRAINT account_email_fkey FOREIGN KEY (email) REFERENCES public.app_user(email);


--
-- Name: inbox_item inbox_item_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbox_item
    ADD CONSTRAINT inbox_item_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(email);


--
-- Name: notification notification_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(email);


--
-- Name: notification notification_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_trip_id_fkey FOREIGN KEY (trip_id) REFERENCES public.trip(id) ON DELETE SET NULL;


--
-- Name: passkey passkey_email_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.passkey
    ADD CONSTRAINT passkey_email_fkey FOREIGN KEY (email) REFERENCES public.account(email) ON DELETE CASCADE;


--
-- Name: reservation reservation_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reservation
    ADD CONSTRAINT reservation_trip_id_fkey FOREIGN KEY (trip_id) REFERENCES public.trip(id) ON DELETE CASCADE;


--
-- Name: trip trip_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trip
    ADD CONSTRAINT trip_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(email);


--
-- Name: web_session web_session_email_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_session
    ADD CONSTRAINT web_session_email_fkey FOREIGN KEY (email) REFERENCES public.account(email) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict dbmate



--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20261023120000'),
    ('20261023140000');
