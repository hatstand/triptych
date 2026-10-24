# Triptych

A TripIt-style itinerary organiser that runs entirely on your own box. Forward or upload confirmation
emails, PDFs or screenshots; a local **Gemma 4** model extracts the reservations, and they're filed
into trips automatically. You get a day-by-day itinerary, an iCal feed, check-in reminders and
"ask about my trip" Q&A.

Typed Python 3.13 (checked with Astral's `ty`, pydantic models). **Postgres** is the system of
record, with hand-written migrations. **Restate** handles durable workflows, ordering, queuing,
timers and retries. **Ollama** runs the model, and **docker compose + Tilt** drive the dev loop.

## Architecture

```
 email ──▶ smtp / /inbound ─┐                  ┌──────────── Restate 1.8 (:8080 ingress, :9070 UI) ────────────┐
                            ├─ workflow_send ▶ │ Ingest workflow: parse ─▶ Llm.extract ─▶ normalise ─▶ User.file │
 browser ─▶ web :8000 ──────┤                  │                         scope "gpu", limit key "batch/<user>"   │
       ▲          │         └─ object_call ──▶ │ User object (key = email): one writer per user, no own state   │
       │          │                            │ Reminders service: delayed sends, deduped by idempotency key   │
       │          │                            │ PlacePhoto object (key = place): Wikimedia photo, fetched once │
       │          │                            │ Assistant service ─▶ Llm.ask (limit key "interactive/<user>")  │
       │          │                            └───────────────── each write = one ctx.run ───────┬─────────────┘
       │          └────────────── reads (plain SQL) ──────────────▶ Postgres 18 ◀─────────────────┘
       └──────────────────────────── HTML / JSON / .ics
```

**Who does what:** Postgres holds trips, reservations, the inbox and notifications, and answers
every read, such as the calendar feed in two queries. Restate never stores app data. It
guarantees each write happens exactly once and in order, and owns everything time- or
failure-related.

| Restate piece | What it does here |
|---|---|
| `Ingest` **workflow** | One per forwarded email or upload. Every step is journaled, so a crash resumes after the last finished step and a successful LLM call never repeats. Inbound mail is keyed by `hash(sender, Message-ID)`, so redeliveries are deduplicated. |
| `Llm` **service** + **flow control** | All model calls go through `ctx.scope("gpu").service_call(..., limit_key="<lane>/<user>")`. On boot the worker writes the rule-book entry `gpu → concurrency = LLM_CONCURRENCY` (default 1). Restate's VQueue scheduler holds the backlog durably and runs it in a fair order: each (lane, user) pair gets its own queue, so one user's 40 emails don't starve anyone else, and interactive Q&A doesn't wait behind the batch backlog. |
| `ctx.run` retry policy | Ollama errors (model still downloading, OOM, malformed JSON) retry with exponential backoff for up to 45 min. After that the inbox item is marked `failed`. |
| `User` **virtual object** | Single writer per user, so two emails for the same trip can't race to create duplicate trips, and a web edit can't interleave with a filing. Each handler is one `ctx.run` around one idempotent Postgres transaction (`db.py`). Retries are safe because trip IDs are UUIDv5s derived from the ingest ID, and a filing that's already been applied is detected and returned as-is. Grouping (`grouping.py`) also **merges** trips when a hotel booking bridges an outbound and a return flight. |
| `PlacePhoto` **virtual object** | One per destination ("Lisbon, Portugal"; keyed by its hash). Filing and removing send it a one-way `find` for any trip place with no photo yet; it takes the freely licensed lead image of the place's English Wikipedia article from Wikimedia Commons, keeps a copy in Postgres (no hotlinking) with its author and licence, and the pages show it with that credit. Then it asks `Llm.locate_subject` (all photos share one `photos` limit key, so a backlog of them takes turns with email extraction rather than flooding the GPU queue) where the photo's main subject is, and pages crop around it. Trips to the same place share it; a place with nothing suitable is remembered as such. Lookups that run out of retries leave no row, and the worker asks again for every place without one when it starts. |
| `Reminders` **service** | Filing schedules **delayed sends** for 24 h and 3 h before a flight and the morning of check-in. It schedules for every reservation in the touched trips, keyed by `reminder:<reservation>:<fire time>`, so Restate deduplicates repeats. A hotel filed before its flight gets its reminder once the flight supplies the timezone. When a reminder fires it re-checks Postgres, so reminders for deleted or rescheduled segments are dropped. |

What's deterministic and what's the LLM: Gemma only turns documents into a flat JSON schema
(`ExtractedReservation`). Times, airport → city/timezone lookup (`airportsdata`), timezone
inheritance for hotels and restaurants, trip grouping and naming are plain, unit-tested Python.

## Quick start

```bash
cp .env.example .env          # defaults target Strix Halo / ROCm
tilt up                       # or: docker compose up -d
```

* App: <http://localhost:8000>. Sign-in is by passkey only. Get your first one from an invite link:
  `docker compose exec web python -m triptych.invite you@example.com` (see [Accounts](#accounts-and-sign-in)).
* Restate UI: <http://localhost:9070> (on a server, see `RESTATE_ADMIN_BIND` in `.env.example`). Invocations, journals, scheduled reminders, and the `gpu` queue (`sys_vqueues`, `sys_user_limits`).
* Postgres: `docker compose exec postgres psql -U triptych`, or `localhost:5432` with user/password `triptych`.
* Tilt UI: <http://localhost:10350>. Use the `send-samples` button to forward the fictional emails in `samples/`.

The first boot pulls the model (~16 GB). Mail sent before the download finishes simply waits in
Restate's retry loop, so you don't need to resend anything.

Pick the LLM backend with `tilt up -- --llm=rocm|vulkan|nvidia|host`, or set `COMPOSE_FILE` in
`.env` for plain compose. `host` uses an Ollama already running on the machine.

Forward mail with any client: SMTP to `localhost:2525`, any recipient. It's filed under the **sender**.

```bash
uv run python scripts/send_samples.py --from me@example.com
```

## Real email: plans@triptych.purplehatstands.com

```
Gmail ─▶ MX: Cloudflare Email Routing ─▶ Email Worker ─HTTPS + bearer token─▶ triptych.purplehatstands.com
      ─▶ web :8000 /inbound  (verified sender + DKIM/ARC check) ─▶ Ingest workflow
```

The Worker posts every message to `INBOUND_URL_TEMPLATE` (`cloudflare/email-worker/wrangler.jsonc`),
which is marvin's public `/inbound` (see `deploy/README.md`, "Public access").

**Test instances over Funnel.** Set the template to
`https://{instance}.axolotl-goblin.ts.net/inbound` and `plans+<instance>@triptych.purplehatstands.com`
is delivered to that tailnet machine, with plain `plans@` going to `DEFAULT_INSTANCE`. Run the stack
there with the same `INBOUND_TOKEN` and `tailscale funnel --bg --set-path /inbound
http://127.0.0.1:8000/inbound`.

* **Hostname pinning:** the Worker substitutes only names matching `[a-z0-9-]` into the template,
  so an address can't send it anywhere else.
* **Existence check:** before delivering, the Worker looks the hostname up via public DNS, so a
  typo'd instance (Tailscale publishes public DNS only for Funnel-enabled machines) bounces with
  *"No Triptych instance …"* rather than being retried.

Nothing listens on port 25 here. The Worker
(`cloudflare/email-worker/`) passes the raw message through byte-for-byte. `/inbound`
(`src/triptych/inbound.py`) then authenticates it:

* **Verified senders:** the canonical envelope sender must be one of a user's verified sending
  addresses, and the message is filed in that user's account. Anything else is refused before any
  DNS lookups. A user's first address is the one their invite named; they add more on the account
  page, which gives them a code to send from the new address to `plans@` (`src/triptych/senders.py`).
* **Manual forwards** must carry a DKIM signature from the sender's own domain.
* **Gmail auto-forwards**, whose envelope sender is rewritten to `you+caf_=…@gmail.com`, must
  carry a valid ARC chain sealed by `google.com`.
* **Gmail's "confirm forwarding" email** is recognised, and its code appears as an alert in the
  web UI.

If Triptych can't take a message (a deploy is restarting it, marvin is offline: anything but a
403 or 413), the Worker accepts it anyway, parks the raw message in the R2 bucket
`triptych-email-parked`, and retries it from the Queue `triptych-email-retries`: 30 s, doubling to
hourly, for about 19 hours (`cloudflare/email-worker/src/deliver.ts`). Delivered messages are
deleted from R2. Nothing bounces and nothing is forwarded to anyone's mailbox. Retries are safe:
`/inbound` dedupes on the message's Message-ID. Both fit Cloudflare's free tiers.

**Replaying parked mail.** If retries run out, the Worker logs `giving up on parked <key>` and
leaves the message in R2. Once Triptych is back, replay it from `cloudflare/email-worker`:

```bash
npx wrangler r2 object get triptych-email-parked/<key> --remote --file msg.eml
curl -sS https://triptych.purplehatstands.com/inbound -H "authorization: Bearer $INBOUND_TOKEN" \
  -H "x-envelope-from: <sender from the log>" -H "content-type: message/rfc822" --data-binary @msg.eml
npx wrangler r2 object delete triptych-email-parked/<key> --remote
```

One-time setup:

1. **DNS on Cloudflare**, with Email Routing enabled for the `triptych.purplehatstands.com`
   subdomain (Email Routing → **Settings → Subdomains**) and catch-all off.
2. **Shared token:** add `INBOUND_TOKEN` (`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`)
   to `.env`. There is no sender allowlist to configure: users' addresses live in Postgres.
3. **Public `/inbound`:** marvin's public site must be up (`deploy/README.md`, "Public access").
4. **Retry storage:** create the R2 bucket and Queue the Worker binds to, once (dashboard → R2 /
   Queues, or `npx wrangler r2 bucket create triptych-email-parked` and
   `npx wrangler queues create triptych-email-retries`).
5. **Create the Worker, deployed from GitHub.** Cloudflare's own CI (Workers Builds) deploys it on
   every merge to `main` that touches it. Its deploy token is created and kept by Cloudflare, so no
   Cloudflare credential is ever stored in GitHub. In the dashboard, go to **Workers & Pages →
   Create → Import a repository**, pick `hatstand/triptych` (approve the Cloudflare GitHub app for
   it the first time), and fill in:
   - **Project name:** `triptych-email`. It must match `name` in `wrangler.jsonc`, or builds fail.
   - **Build command:** empty. **Deploy command:** `npm run deploy` (type-checks, then `wrangler deploy`).
   - **Preview command:** leave the default; **Enable Preview builds:** off, so branches never build.
   - **Advanced settings → Path:** `cloudflare/email-worker`
   - **API token:** *Create new token* (the one Cloudflare's builds use).
   - **Variable name/value:** empty. These are build-time variables, not the Worker's secrets.

   Then, on the Worker:
   - **Settings → Variables and secrets → Add:** `INBOUND_TOKEN`, type *Secret*, the same value as
     `.env`. Secrets survive deploys; the plain vars come from `wrangler.jsonc` on each deploy.
   - **Settings → Build → Build watch paths:** include `cloudflare/email-worker/*`, so other
     merges don't redeploy it.
   - **Settings → Build → Branch control:** check the production branch is `main`.

   Pull requests are still checked by `.github/workflows/email-worker.yml` (lint, types, tests).
   To deploy by hand instead: `npx wrangler login && npm ci && npm run deploy` in that directory.
6. **Routing:** in Cloudflare, go to Email Routing.
   - **Settings:** turn on **Subaddressing**, so the `plans@` rule also matches `plans+<instance>@`.
   - **Routing rules:** set `plans@` to *Send to a Worker* → `triptych-email`.
7. **Gmail:**
   - *Optional, automatic forwarding:* go to Settings → Forwarding, add `plans@triptych.purplehatstands.com`,
     and read the confirmation code from Triptych's alerts. Then add a filter (e.g. `subject:(booking
     OR itinerary OR confirmation)`) that forwards to it.
   - *Manual:* forward any confirmation to `plans@triptych.purplehatstands.com`.

## Running on a Ryzen AI MAX 385 (Radeon 8050S, 32 GB)

**Model:** `gemma4:26b-a4b-it-qat`. It's a mixture-of-experts model with ~4 B parameters active per
token, so decoding speed is set by memory bandwidth rather than compute. The QAT 4-bit weights are
~16 GB, which leaves room for the OS, Restate and the app on a 32 GB machine. Ollama is configured
for 1 resident model, 1 parallel request, 16 K context and a q8 KV cache (`compose/ollama.yaml`).
If memory is tight, set `LLM_MODEL=gemma4:e4b-it-qat` (~6 GB).

**GPU memory:** the iGPU shares system RAM. Keep the BIOS "UMA frame buffer" small (0.5–2 GB)
and let the driver use GTT. Make sure the GTT limit covers the model and KV cache: on recent
kernels add `ttm.pages_limit=6291456 ttm.page_pool_size=6291456` (24 GiB) to the kernel command
line. Check the result with `cat /sys/class/drm/card*/device/mem_info_gtt_total`. After the first
request, `docker compose exec ollama ollama ps` should show **100% GPU**. If it shows a CPU split,
the GPU can't see enough memory.

**ROCm vs Vulkan:** the default is the `ollama/ollama:0.40.9-rocm` image with `/dev/kfd` and
`/dev/dri`. On Strix Halo, Vulkan (RADV) is often as fast or faster for token generation and avoids
ROCm version churn: `tilt up -- --llm=vulkan`. Compare both. If ROCm logs say gfx1151 is
unsupported, uncomment `HSA_OVERRIDE_GFX_VERSION` in `compose/gpu-rocm.yaml`. Your user needs to
be in the `video` and `render` groups.

**Concurrency:** keep `LLM_CONCURRENCY=1` on this hardware. Raising it raises the Restate rule
and `OLLAMA_NUM_PARALLEL` together, but on one iGPU that mostly adds KV-cache memory without
adding throughput.

## Database migrations

The migrations are hand-written SQL in `db/migrations/`, applied by [dbmate](https://github.com/amacneil/dbmate)
and linted by [squawk](https://squawkhq.com).

```bash
scripts/dbmate.sh new add_trip_notes     # creates db/migrations/<ts>_add_trip_notes.sql to fill in
# Under Tilt, saving it applies it (db-schema resource) and refreshes db/schema.sql; commit both.
scripts/dbmate.sh up | rollback | status
uv run python scripts/lint_migrations.py
```

* **Startup:** the compose `migrate` service applies pending migrations, and `worker`/`web` wait for it.
* **Linting:** `scripts/lint_migrations.py` runs squawk with dbmate's semantics. Only the
  `-- migrate:up` section is linted, and each file counts as transactional unless it's marked
  `transaction:false`. So squawk demands `CONCURRENTLY` for indexes on existing tables, and
  re-runnable statements outside a transaction.
* **Conventions:** start each file with `SET lock_timeout` and `SET statement_timeout`. Put each
  `CONCURRENTLY` operation in its own `-- migrate:up transaction:false` file, and
  `DROP INDEX CONCURRENTLY IF EXISTS` first, because a failed build leaves an INVALID index behind.

## Accounts and sign-in

Users sign in with passkeys (WebAuthn) and nothing else: no passwords, no email links.

A **user** is an `app_user` id (a UUID). Everything a user owns (trips, inbox, notifications,
passkeys, sessions) is keyed by that id, as is the Restate `User` object. Email addresses live in
`user_email` and point at a user, so a user can have several, and SSO identities can be added the
same way later. Inbound mail is matched on its DKIM-verified sender address: an address nobody has
seen before gets a new user, so mail can be filed before its owner ever signs in, and those trips
are there when they do.

Nothing proves that someone controls an address, so the first passkey needs an **invite**: a
one-time link, valid for 7 days, for one of the user's addresses, printed by

```bash
docker compose exec web python -m triptych.invite you@example.com [--days 1]
```

or made from the **Invites** form on the Tailscale-authorised `/admin` page (deploy/README.md).

That's how the first user signs up, how anyone else is let in, and how someone who has lost every
passkey gets back in. Signed-in users add more passkeys (a phone, a security key) from the account
page, and can remove any passkey except their last.

* **RP ID and origins.** Passkeys are bound to the RP ID: by default the hostname of
  `PUBLIC_BASE_URL`, which must be exactly what the browser's address bar shows. Ceremonies from
  any other origin fail, so passkeys don't work via a bare IP or another hostname. Set
  `WEBAUTHN_RP_ID` to a parent domain if the UI might move between subdomains, because changing
  the RP ID later strands every registered passkey. `ts.net` is a public suffix, so it can't be
  the RP ID itself. `WEBAUTHN_ORIGINS` (comma-separated) lists extra origins to accept.
* **Sessions** are random tokens in an HttpOnly, SameSite=Lax cookie, `__Host-session` over
  HTTPS. Only their SHA-256 is stored (`web_session`), so signing out ends them server-side. They
  last 30 days.
* **CSRF.** POSTs whose `Origin` isn't one of the allowed origins get a 403 (`/inbound` excepted).
* **Challenges** are stateless: the challenge, its purpose, the user and the invite travel in a
  5-minute cookie signed with `SECRET_KEY` (so set a real one). A challenge isn't strictly
  single-use within that window, but replaying one needs both that cookie and the authenticator's
  response from the same ceremony. Invites are still spent in the database.
* **WebAuthn user handle** is the 16 bytes of the user's UUID: stable, random, and not an address.

## Logs and traces

Logging is stdlib `logging` and tracing is OpenTelemetry (`src/triptych/telemetry.py`, set up in
each process's lifespan or `main`).

* **Logs** go to stdout as one JSON object per line, with OpenTelemetry log data model names where
  there is one, the process and the deployed image, and `trace_id` and `span_id` whenever the line
  was written inside a span, so `docker compose logs worker | jq` lines up with a trace:

  ```json
  {"severity_text": "INFO", "logger": "triptych.worker", "message": "registered …", "service": "triptych-worker", "version": "ghcr.io/hatstand/triptych:<sha>", "trace_id": "4bf9…", "span_id": "00f0…", "timestamp": "2026-10-24T17:12:07.367596+00:00"}
  ```

  Fields passed with `extra={...}` become keys of their own. uvicorn's and hypercorn's own logs
  (access log included) come out the same way. `LOG_FORMAT=text` prints plain lines instead, which
  Tilt uses (`compose/dev.yaml`), and `LOG_LEVEL` sets the level (default `INFO`).
* **Traces**: a server span per web/admin request (FastAPI), per Restate handler attempt in the
  worker and per SMTP message, with child spans for every httpx call (Ollama, the Restate client,
  Wikimedia) and Postgres query (psycopg). Each model call is a `chat <model>` span with the
  [GenAI semantic conventions](https://opentelemetry.io/docs/specs/semconv/gen-ai/) (model, token
  usage, finish reason) plus `triptych.prompt.name` and `triptych.prompt.version` (a hash of the
  system prompt and output schema, the same as an extraction run's `prompt_version`).
* **Export** is OTLP over HTTP and **off until an endpoint is set**. Everything uses the standard
  variables, so any OTLP backend works, hosted or self-hosted:

  ```bash
  OTEL_EXPORTER_OTLP_ENDPOINT=https://otlp.example.com      # traces → /v1/traces, logs → /v1/logs
  OTEL_EXPORTER_OTLP_HEADERS=authorization=Bearer%20<token> # if the backend wants one
  OTEL_RESOURCE_ATTRIBUTES=deployment.environment.name=marvin
  ```

  Per-signal variables (`OTEL_EXPORTER_OTLP_TRACES_ENDPOINT`, `OTEL_TRACES_EXPORTER=none`, …),
  sampling (`OTEL_TRACES_SAMPLER`) and `OTEL_SDK_DISABLED=true` work as the OTel spec says. Service
  names are `triptych-web`, `triptych-worker`, `triptych-smtp` and `triptych-admin`.
* **Through Restate**: the Restate client sends W3C `traceparent` to the ingress, and Restate hands
  each handler attempt its trace context, which the worker continues. Restate only continues the
  caller's trace when it traces too, so for one trace from the browser request through `Ingest`
  and `Llm.extract` to Ollama, also give the `restate` service `RESTATE_TRACING_ENDPOINT` (OTLP/gRPC
  like `http://collector:4317`, or `otlp+http://collector:4318/v1/traces`); it then adds its own
  spans for ingress, each invocation and each attempt. Without it, each invocation starts its own
  trace in the worker. Compose can't pass it empty (Restate refuses to start), so it is set in
  `compose/otel.yaml` rather than `compose.yaml`; for another backend, set it the same way in a
  compose override.

**Email Worker:** Workers Logs and Cloudflare's automatic OpenTelemetry tracing are on in
`wrangler.jsonc`. Cloudflare can export both over OTLP to the same backend: add it under Workers
Observability → Destinations and list it in `observability.traces.destinations` /
`observability.logs.destinations`.

**Self-hosted:** append `:compose/otel.yaml` to `COMPOSE_FILE` (or pass `-f compose/otel.yaml`) to
run [`grafana/otel-lgtm`](https://github.com/grafana/docker-otel-lgtm), a single container with an
OTel Collector, Tempo (traces), Loki (logs), Prometheus and Grafana, at <http://localhost:3000>.
It wires up the app and Restate for you. It's fine for one box; for anything longer-lived, run a
standalone OpenTelemetry Collector and point it wherever the data should end up.

## Development

```bash
uv sync                          # Python 3.13 venv with dev tools
uv run pytest -q                 # unit tests; DB tests too if TEST_DATABASE_URL is set
TEST_DATABASE_URL=postgresql://triptych:triptych@127.0.0.1:5432/postgres uv run pytest -q
uv run ty check && uv run ruff check src tests
```

Under Tilt, `./src` is live-synced into the containers. The worker restarts and **re-registers
with Restate on every start**, so new or changed handlers are picked up within a second or two.
The `check` resource runs ruff, ty, the migration linter and pytest on every save. The DB tests
build a throwaway database from `db/schema.sql` on the compose Postgres and drop it afterwards.

GitHub Actions runs the same checks on every pull request: ruff (lint + format check), ty and
pytest (against a Postgres service container, so the DB tests run too) for the Python code,
Biome lint, `tsc` and a Vite build for the browser code in `frontend/`, a Biome format + lint check of
`frontend/src/*.css` (fix with `npm run format:css` in `frontend/`), and Biome lint, `tsc` and `node --test`
for the Email Worker (`npm run lint && npm run typecheck && npm test` in `cloudflare/email-worker`).

Each pull request also gets a comment with light and dark screenshots of the login, home, trip,
account and admin pages, next to main's (`.github/workflows/screenshots.yml`).
`scripts/screenshots.py` seeds an empty database with fixture trips and an inbox, runs the web and
admin apps and captures them with headless Chromium; `scripts/post_screenshots.sh` commits the
images to the `screenshots` branch and updates the comment. To take them locally, against a
scratch database and with the frontend built:

```bash
createdb -h 127.0.0.1 -U triptych screenshots
uv run playwright install chromium
DATABASE_URL=postgresql://triptych:triptych@127.0.0.1:5432/screenshots uv run python scripts/screenshots.py screenshots
```

The browser code is TypeScript in `frontend/src`, one entry per page script, bundled by Vite
(with its npm dependencies, such as htmx) into `src/triptych/web/static/assets/<name>-<hash>.js`,
which is gitignored; `tsc` only type-checks. The stylesheet (`frontend/src/style.css`) and the icon
sprite go through the same build. Every file name carries a content hash, so templates link them
with `static('live.ts')` / `static('style.css')`, which looks the hashed name up in Vite's manifest,
and the web app serves `/static/assets/` as immutable for a year (safe behind Cloudflare's cache). The Dockerfile builds it in a Node stage; under Tilt the
`frontend` resource runs `vite build --watch` and the output is live-synced like any other file in
`src`. Without Tilt: `cd frontend && npm ci && npm run build`. A new page script goes in
`frontend/src` and in `ENTRIES` in `frontend/vite.config.ts`.

Layout:

```
src/triptych/
  models.py        pydantic models for every Restate payload and state value
  parsing.py       .eml / HTML / PDF (text, or rasterised if scanned) / image → LLM input
  llm.py           Ollama client: structured-output extraction + Q&A
  telemetry.py     logging (JSON to stdout) + OpenTelemetry tracing and OTLP export
  enrich.py        times, airports → city/tz, tz inheritance
  grouping.py      reservation → trip assignment and trip merging (pure)
  ical.py          .ics export
  db.py            Postgres access: plain SQL → pydantic, idempotent writes for ctx.run
  auth.py          passkeys (WebAuthn ceremonies), sessions, invites
  invite.py        CLI: print a one-time passkey registration link for an address
  photos.py        destination photos: choose on Wikipedia/Commons, download
  services/        Restate handlers: ingest, llm_service, user, place_photo, reminders, assistant, endpoint
  worker.py        Restate endpoint (hypercorn h2c) + self-registration + flow-control rules
  web/             FastAPI + Jinja UI and JSON API (static/ is built from frontend/)
  smtp.py          inbound mail gateway
frontend/          TypeScript and CSS for the browser (Vite → src/triptych/web/static)
db/                migrations/ (hand-written SQL) + schema.sql (generated, committed)
compose.yaml       postgres, migrate, restate, worker, web, smtp
compose/           ollama.yaml + gpu-{rocm,vulkan,nvidia}.yaml | host-llm.yaml, dev.yaml (hot reload),
                   otel.yaml (opt-in Grafana for traces and logs)
```

## Prototype limitations

* A user can have several addresses, but nothing in the UI adds a second one yet (insert into
  `user_email`). The calendar feed is HMAC-signed with `SECRET_KEY`.
* No live flight status. Alerts are time-based reminders only.
* Trip grouping is date-based with a 1-day slack, plus confirmation-code and bridging merges.
  Edit or delete segments in the UI when it guesses wrong.
* Reminder notifications appear in the web UI. There is no email or push delivery yet.
