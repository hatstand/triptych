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
| `Reminders` **service** | Filing schedules **delayed sends** for 24 h and 3 h before a flight and the morning of check-in. It schedules for every reservation in the touched trips, keyed by `reminder:<reservation>:<fire time>`, so Restate deduplicates repeats. A hotel filed before its flight gets its reminder once the flight supplies the timezone. When a reminder fires it re-checks Postgres, so reminders for deleted or rescheduled segments are dropped. |

What's deterministic and what's the LLM: Gemma only turns documents into a flat JSON schema
(`ExtractedReservation`). Times, airport → city/timezone lookup (`airportsdata`), timezone
inheritance for hotels and restaurants, trip grouping and naming are plain, unit-tested Python.

## Quick start

```bash
cp .env.example .env          # defaults target Strix Halo / ROCm
tilt up                       # or: docker compose up -d
```

* App: <http://localhost:8000>. Sign in with the address you'll forward mail from (prototype, no password).
* Restate UI: <http://localhost:9070>. Invocations, journals, scheduled reminders, and the `gpu` queue (`sys_vqueues`, `sys_user_limits`).
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

## Real email: plans@purplehatstands.com

```
Gmail ─▶ MX: Cloudflare Email Routing ─▶ Email Worker ─HTTPS + bearer token─▶ Tailscale Funnel
      ─▶ web :8000 /inbound  (allowlist + DKIM/ARC check) ─▶ Ingest workflow
```

**Several instances, one address.** `plans+<instance>@purplehatstands.com` is delivered to
`https://<instance>.axolotl-goblin.ts.net/inbound`. Plain `plans@` goes to the default instance (`zem`).
To add a test instance, just run the stack on another tailnet machine with the same
`INBOUND_TOKEN` and turn on Funnel there; no Cloudflare changes are needed.

* **Hostname pinning:** the Worker substitutes only names matching `[a-z0-9-]` into a template
  pinned to the tailnet's domain, so an address can't send it anywhere else.
* **Existence check:** before delivering, the Worker looks the hostname up via public DNS.
  Tailscale publishes public DNS only for Funnel-enabled machines, so a typo or a machine without
  Funnel bounces with *"No Triptych instance …"* rather than going to the fallback mailbox.

Nothing listens on port 25 here, and Funnel exposes only the `/inbound` path. The Worker
(`cloudflare/email-worker/`) passes the raw message through byte-for-byte. `/inbound`
(`src/triptych/inbound.py`) then authenticates it:

* **Allowlist:** the canonical envelope sender must be in `INBOUND_ALLOWED_SENDERS`.
* **Manual forwards** must carry a DKIM signature from the sender's own domain.
* **Gmail auto-forwards**, whose envelope sender is rewritten to `you+caf_=…@gmail.com`, must
  carry a valid ARC chain sealed by `google.com`.
* **Gmail's "confirm forwarding" email** is recognised, and its code appears as an alert in the
  web UI.

If zem can't be reached, the Worker retries and then forwards the message to `FALLBACK_ADDRESS`,
so nothing is lost.

One-time setup:

1. **DNS on Cloudflare**, with Email Routing enabled and catch-all off.
2. **Shared token:** add `INBOUND_TOKEN` (`python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`)
   and `INBOUND_ALLOWED_SENDERS` to `.env`.
3. **Funnel:** `tailscale funnel --bg --set-path /inbound http://127.0.0.1:8000/inbound`
4. **Deploy the Worker:**
   ```bash
   cd cloudflare/email-worker && npm ci
   npx wrangler login
   npx wrangler secret put INBOUND_TOKEN      # same value as .env
   npm test && npm run deploy
   ```
5. **Routing:** in Cloudflare, go to Email Routing.
   - **Settings:** turn on **Subaddressing**, so the `plans@` rule also matches `plans+<instance>@`.
   - **Destination addresses:** verify `FALLBACK_ADDRESS`.
   - **Routing rules:** set `plans@` to *Send to a Worker* → `triptych-email`.
6. **Gmail:**
   - *Optional, automatic forwarding:* go to Settings → Forwarding, add `plans@purplehatstands.com`,
     and read the confirmation code from Triptych's alerts. Then add a filter (e.g. `subject:(booking
     OR itinerary OR confirmation) -to:john.maguire+triptych-undelivered@gmail.com`) that forwards
     to it. The `-to:` exclusion keeps fallback copies from being forwarded back.
   - *Manual:* forward any confirmation to `plans@purplehatstands.com`.

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

Layout:

```
src/triptych/
  models.py        pydantic models for every Restate payload and state value
  parsing.py       .eml / HTML / PDF (text, or rasterised if scanned) / image → LLM input
  llm.py           Ollama client: structured-output extraction + Q&A
  enrich.py        times, airports → city/tz, tz inheritance
  grouping.py      reservation → trip assignment and trip merging (pure)
  ical.py          .ics export
  db.py            Postgres access: plain SQL → pydantic, idempotent writes for ctx.run
  services/        Restate handlers: ingest, llm_service, user, reminders, assistant, endpoint
  worker.py        Restate endpoint (hypercorn h2c) + self-registration + flow-control rules
  web/             FastAPI + Jinja UI and JSON API
  smtp.py          inbound mail gateway
db/                migrations/ (hand-written SQL) + schema.sql (generated, committed)
compose.yaml       postgres, migrate, restate, worker, web, smtp
compose/           ollama.yaml + gpu-{rocm,vulkan,nvidia}.yaml | host-llm.yaml, dev.yaml (hot reload)
```

## Prototype limitations

* No real auth: the login is a cookie holding your email, and inbound mail trusts the `From`
  address. The calendar feed is HMAC-signed with `SECRET_KEY`.
* No live flight status. Alerts are time-based reminders only.
* Trip grouping is date-based with a 1-day slack, plus confirmation-code and bridging merges.
  Edit or delete segments in the UI when it guesses wrong.
* Reminder notifications appear in the web UI. There is no email or push delivery yet.
