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
-- Name: app_user; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.app_user (
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    id uuid DEFAULT gen_random_uuid() NOT NULL
);


--
-- Name: dismissed_reservation; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.dismissed_reservation (
    owner uuid NOT NULL,
    ingest_id text NOT NULL,
    dedupe_key text NOT NULL,
    kind text NOT NULL,
    confirmation_code text,
    start_date date NOT NULL,
    city text,
    dismissed_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: eval_copy; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.eval_copy (
    ingest_id text NOT NULL,
    owner uuid NOT NULL,
    copied_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: eval_share; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.eval_share (
    user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: extraction_example; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extraction_example (
    ingest_id text NOT NULL,
    owner uuid NOT NULL,
    sender_domain text,
    subject text,
    text text NOT NULL,
    answer jsonb NOT NULL,
    changes text[] DEFAULT '{}'::text[] NOT NULL,
    partial boolean DEFAULT false NOT NULL,
    live_at timestamp with time zone NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: extraction_run; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.extraction_run (
    id uuid NOT NULL,
    ingest_id text NOT NULL,
    owner uuid NOT NULL,
    input_id uuid,
    model text NOT NULL,
    model_digest text,
    prompt_version text NOT NULL,
    code_version text NOT NULL,
    params jsonb NOT NULL,
    purpose text NOT NULL,
    output jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    examples text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT extraction_run_purpose_check CHECK ((purpose = ANY (ARRAY['ingest'::text, 'reprocess'::text, 'shadow'::text, 'eval'::text])))
);


--
-- Name: inbox_item; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.inbox_item (
    ingest_id text NOT NULL,
    owner uuid NOT NULL,
    source text NOT NULL,
    label text NOT NULL,
    received_at timestamp with time zone NOT NULL,
    state text NOT NULL,
    message text,
    trip_ids uuid[] DEFAULT '{}'::uuid[] NOT NULL,
    input_id uuid,
    current_run_id uuid
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
    owner uuid NOT NULL,
    at timestamp with time zone NOT NULL,
    trip_id uuid,
    text text NOT NULL
);


--
-- Name: passkey; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.passkey (
    credential_id bytea NOT NULL,
    user_id uuid NOT NULL,
    public_key bytea NOT NULL,
    sign_count bigint NOT NULL,
    transports text[] DEFAULT '{}'::text[] NOT NULL,
    backed_up boolean NOT NULL,
    name text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    last_used_at timestamp with time zone
);


--
-- Name: place_photo; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.place_photo (
    place text NOT NULL,
    image bytea,
    content_type text,
    file text,
    page_url text,
    article_url text,
    artist text,
    licence text,
    licence_url text,
    fetched_at timestamp with time zone DEFAULT now() NOT NULL,
    aspect real,
    subject real[],
    subject_checked boolean DEFAULT false NOT NULL
);


--
-- Name: raw_input; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.raw_input (
    id uuid NOT NULL,
    owner uuid NOT NULL,
    sha256 bytea NOT NULL,
    object_key text NOT NULL,
    size bigint NOT NULL,
    source text NOT NULL,
    content_type text NOT NULL,
    filename text,
    received_at timestamp with time zone NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: reprocess; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.reprocess (
    id text NOT NULL,
    batch_id text NOT NULL,
    ingest_id text NOT NULL,
    owner uuid NOT NULL,
    mode text NOT NULL,
    model text,
    from_run_id uuid,
    run_id uuid,
    state text DEFAULT 'queued'::text NOT NULL,
    diff jsonb,
    error text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    finished_at timestamp with time zone,
    CONSTRAINT reprocess_mode_check CHECK ((mode = ANY (ARRAY['dry_run'::text, 'apply'::text, 'shadow'::text]))),
    CONSTRAINT reprocess_state_check CHECK ((state = ANY (ARRAY['queued'::text, 'done'::text, 'failed'::text])))
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
    seat text,
    travelers text[] DEFAULT '{}'::text[] NOT NULL,
    notes text,
    source_ingest_id text,
    dedupe_key text NOT NULL,
    source_run_id uuid,
    service_number text,
    edited_fields text[] DEFAULT '{}'::text[] NOT NULL,
    CONSTRAINT reservation_kind_check CHECK ((kind = ANY (ARRAY['flight'::text, 'lodging'::text, 'car'::text, 'rail'::text, 'ferry'::text, 'restaurant'::text, 'activity'::text, 'other'::text])))
);


--
-- Name: schema_migrations; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.schema_migrations (
    version character varying NOT NULL
);


--
-- Name: sender_verification; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.sender_verification (
    code_hash bytea NOT NULL,
    user_id uuid NOT NULL,
    email text NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: trip; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.trip (
    id uuid NOT NULL,
    owner uuid NOT NULL,
    name text NOT NULL,
    destination text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    name_edited boolean DEFAULT false NOT NULL,
    place text
);


--
-- Name: user_email; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_email (
    email text NOT NULL,
    user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: user_key; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.user_key (
    user_id uuid NOT NULL,
    aead_keyset bytea NOT NULL,
    prf_keyset bytea NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: web_session; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.web_session (
    token_hash bytea NOT NULL,
    user_id uuid NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    expires_at timestamp with time zone NOT NULL
);


--
-- Name: app_user app_user_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.app_user
    ADD CONSTRAINT app_user_pkey PRIMARY KEY (id);


--
-- Name: dismissed_reservation dismissed_reservation_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dismissed_reservation
    ADD CONSTRAINT dismissed_reservation_pkey PRIMARY KEY (owner, ingest_id, dedupe_key);


--
-- Name: eval_copy eval_copy_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_copy
    ADD CONSTRAINT eval_copy_pkey PRIMARY KEY (ingest_id);


--
-- Name: eval_share eval_share_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_share
    ADD CONSTRAINT eval_share_pkey PRIMARY KEY (user_id);


--
-- Name: extraction_example extraction_example_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_example
    ADD CONSTRAINT extraction_example_pkey PRIMARY KEY (ingest_id);


--
-- Name: extraction_run extraction_run_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_run
    ADD CONSTRAINT extraction_run_pkey PRIMARY KEY (id);


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
-- Name: place_photo place_photo_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.place_photo
    ADD CONSTRAINT place_photo_pkey PRIMARY KEY (place);


--
-- Name: raw_input raw_input_owner_sha256_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.raw_input
    ADD CONSTRAINT raw_input_owner_sha256_key UNIQUE (owner, sha256);


--
-- Name: raw_input raw_input_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.raw_input
    ADD CONSTRAINT raw_input_pkey PRIMARY KEY (id);


--
-- Name: reprocess reprocess_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reprocess
    ADD CONSTRAINT reprocess_pkey PRIMARY KEY (id);


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
-- Name: sender_verification sender_verification_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sender_verification
    ADD CONSTRAINT sender_verification_pkey PRIMARY KEY (code_hash);


--
-- Name: trip trip_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trip
    ADD CONSTRAINT trip_pkey PRIMARY KEY (id);


--
-- Name: user_email user_email_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_email
    ADD CONSTRAINT user_email_pkey PRIMARY KEY (email);


--
-- Name: user_key user_key_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_key
    ADD CONSTRAINT user_key_pkey PRIMARY KEY (user_id);


--
-- Name: web_session web_session_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_session
    ADD CONSTRAINT web_session_pkey PRIMARY KEY (token_hash);


--
-- Name: eval_copy_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX eval_copy_owner_idx ON public.eval_copy USING btree (owner);


--
-- Name: extraction_example_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX extraction_example_owner_idx ON public.extraction_example USING btree (owner, sender_domain);


--
-- Name: extraction_run_ingest_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX extraction_run_ingest_idx ON public.extraction_run USING btree (ingest_id);


--
-- Name: extraction_run_input_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX extraction_run_input_idx ON public.extraction_run USING btree (input_id);


--
-- Name: extraction_run_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX extraction_run_owner_idx ON public.extraction_run USING btree (owner);


--
-- Name: inbox_item_owner_received_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX inbox_item_owner_received_idx ON public.inbox_item USING btree (owner, received_at DESC);


--
-- Name: notification_owner_at_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX notification_owner_at_idx ON public.notification USING btree (owner, at DESC);


--
-- Name: passkey_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX passkey_user_idx ON public.passkey USING btree (user_id);


--
-- Name: reprocess_batch_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX reprocess_batch_idx ON public.reprocess USING btree (batch_id);


--
-- Name: reprocess_ingest_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX reprocess_ingest_idx ON public.reprocess USING btree (ingest_id);


--
-- Name: reservation_trip_start_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX reservation_trip_start_idx ON public.reservation USING btree (trip_id, start_local);


--
-- Name: sender_verification_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX sender_verification_user_idx ON public.sender_verification USING btree (user_id);


--
-- Name: trip_owner_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX trip_owner_idx ON public.trip USING btree (owner);


--
-- Name: user_email_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX user_email_user_idx ON public.user_email USING btree (user_id);


--
-- Name: web_session_user_idx; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX web_session_user_idx ON public.web_session USING btree (user_id);


--
-- Name: dismissed_reservation dismissed_reservation_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.dismissed_reservation
    ADD CONSTRAINT dismissed_reservation_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: eval_copy eval_copy_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_copy
    ADD CONSTRAINT eval_copy_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: eval_share eval_share_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.eval_share
    ADD CONSTRAINT eval_share_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: extraction_example extraction_example_ingest_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_example
    ADD CONSTRAINT extraction_example_ingest_id_fkey FOREIGN KEY (ingest_id) REFERENCES public.inbox_item(ingest_id) ON DELETE CASCADE;


--
-- Name: extraction_example extraction_example_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_example
    ADD CONSTRAINT extraction_example_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: extraction_run extraction_run_input_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_run
    ADD CONSTRAINT extraction_run_input_id_fkey FOREIGN KEY (input_id) REFERENCES public.raw_input(id) ON DELETE CASCADE;


--
-- Name: extraction_run extraction_run_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.extraction_run
    ADD CONSTRAINT extraction_run_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: inbox_item inbox_item_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.inbox_item
    ADD CONSTRAINT inbox_item_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(id);


--
-- Name: notification notification_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(id);


--
-- Name: notification notification_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification
    ADD CONSTRAINT notification_trip_id_fkey FOREIGN KEY (trip_id) REFERENCES public.trip(id) ON DELETE SET NULL;


--
-- Name: passkey passkey_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.passkey
    ADD CONSTRAINT passkey_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: raw_input raw_input_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.raw_input
    ADD CONSTRAINT raw_input_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: reprocess reprocess_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reprocess
    ADD CONSTRAINT reprocess_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: reservation reservation_trip_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.reservation
    ADD CONSTRAINT reservation_trip_id_fkey FOREIGN KEY (trip_id) REFERENCES public.trip(id) ON DELETE CASCADE;


--
-- Name: sender_verification sender_verification_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.sender_verification
    ADD CONSTRAINT sender_verification_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: trip trip_owner_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.trip
    ADD CONSTRAINT trip_owner_fkey FOREIGN KEY (owner) REFERENCES public.app_user(id);


--
-- Name: user_email user_email_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_email
    ADD CONSTRAINT user_email_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: user_key user_key_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.user_key
    ADD CONSTRAINT user_key_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- Name: web_session web_session_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.web_session
    ADD CONSTRAINT web_session_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.app_user(id) ON DELETE CASCADE;


--
-- PostgreSQL database dump complete
--

\unrestrict dbmate



--
-- Dbmate schema migrations
--

INSERT INTO public.schema_migrations (version) VALUES
    ('20261023120000'),
    ('20261023140000'),
    ('20261023150000'),
    ('20261023170000'),
    ('20261023180000'),
    ('20261023190000'),
    ('20261024120000'),
    ('20261024160000'),
    ('20261024170000'),
    ('20261024210000'),
    ('20261024220000'),
    ('20261025120000'),
    ('20261025140000'),
    ('20261025150000'),
    ('20261025220000'),
    ('20261025230000'),
    ('20261026120000'),
    ('20261026140000');
