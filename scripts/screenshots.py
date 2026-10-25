"""Screenshots of the main pages, in light and dark, for pull requests (.github/workflows/screenshots.yml).

Loads db/schema.sql into DATABASE_URL, which must be an empty database, seeds it with one user's
trips, inbox and alerts, runs the web and admin apps, and has headless Chromium save each page twice:

    <out>/light/01-login.png … and <out>/dark/01-login.png …

Run it locally against a scratch database (it refuses one that already has tables):

    createdb -h 127.0.0.1 -U triptych screenshots
    DATABASE_URL=postgresql://triptych:triptych@127.0.0.1:5432/screenshots \\
        uv run python scripts/screenshots.py screenshots

Needs the frontend built (npm run build in frontend/) and Playwright's Chromium (`uv run playwright
install chromium`), or PLAYWRIGHT_CHROMIUM to point at another Chromium binary.
"""

import argparse
import asyncio
import os
import sys
import uuid
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import LiteralString, cast

import httpx
import psycopg
from playwright.async_api import Browser, BrowserContext, Route, ViewportSize, async_playwright

from triptych import auth, db, photos
from triptych.config import settings
from triptych.models import InboxItem, IngestState, Kind, Notification, ReprocessRequest, Reservation
from triptych.services import place_photo

ROOT = Path(__file__).resolve().parent.parent
SCHEMA = ROOT / "db" / "schema.sql"
WEB = "http://127.0.0.1:8100"
ADMIN = "http://127.0.0.1:8101"
EMAIL = "sam@example.com"
ADMIN_LOGIN = "admin@example.com"
BATCH = "dry-run-1"  # the reprocess batch the admin page opens
VIEWPORT: ViewportSize = {"width": 1280, "height": 800}
SHORT: ViewportSize = {"width": 1280, "height": 700}  # a laptop with browser chrome; home's sidebar unpins


def load_schema(url: str) -> None:
    with psycopg.connect(url, autocommit=True) as conn:
        cur = conn.execute("SELECT count(*) FROM pg_tables WHERE schemaname = 'public'")
        if (row := cur.fetchone()) and row[0]:
            sys.exit(f"{url} already has tables: point DATABASE_URL at an empty database")
        # pg_dump ≥ 17.6 wraps dumps in psql-only \restrict lines; strip meta-commands (as conftest.py).
        ddl = "\n".join(line for line in SCHEMA.read_text().splitlines() if not line.startswith("\\"))
        conn.execute(cast(LiteralString, ddl))  # our own generated schema dump


def at(day: date, hh_mm: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(hh_mm))


def res(kind: Kind, title: str, start: datetime, end: datetime | None = None, **kw: str) -> Reservation:
    return Reservation(id=str(uuid.uuid4()), kind=kind, title=title, start_local=start, end_local=end, **kw)


async def seed() -> str:
    """One user with an upcoming trip (connecting flights, hotel, dinner, a ferry), a later one and a
    past one, plus an inbox in every state and an alert. Dates are relative to today, so the home
    page always has upcoming and past trips. Returns a session token for the user."""
    me = await db.user_for_email(EMAIL)
    d = date.today() + timedelta(days=12)
    lisbon = [
        res(
            Kind.FLIGHT,
            "BA 286 SFO→LHR",
            at(d, "19:40"),
            at(d + timedelta(days=1), "13:55"),
            provider="British Airways",
            confirmation_code="XK4T9Q",
            start_location="SFO",
            end_location="LHR",
            start_tz="America/Los_Angeles",
            end_tz="Europe/London",
            city="London",
            service_number="BA286",
            seat="23A",
        ),
        res(
            Kind.FLIGHT,
            "BA 500 LHR→LIS",
            at(d + timedelta(days=1), "16:10"),
            at(d + timedelta(days=1), "18:50"),
            provider="British Airways",
            confirmation_code="XK4T9Q",
            start_location="LHR",
            end_location="LIS",
            start_tz="Europe/London",
            end_tz="Europe/Lisbon",
            city="Lisbon",
            service_number="BA500",
            seat="14C",
        ),
        res(
            Kind.LODGING,
            "Casa do Miradouro",
            # Check-in opens before the flight lands: the timeline still puts the hotel after it.
            at(d + timedelta(days=1), "15:00"),
            at(d + timedelta(days=5), "11:00"),
            provider="Casa do Miradouro",
            confirmation_code="CM-20419",
            address="Rua de São Pedro de Alcântara 45, Lisboa",
            city="Lisbon",
            start_tz="Europe/Lisbon",
            notes="Double room, river view",
        ),
        res(
            Kind.RESTAURANT,
            "Taberna da Rua das Flores",
            at(d + timedelta(days=2), "20:30"),
            address="Rua das Flores 103, Lisboa",
            city="Lisbon",
            notes="Table for 2",
        ),
        res(
            Kind.ACTIVITY,
            "Sintra palaces day tour",
            at(d + timedelta(days=3), "09:00"),
            at(d + timedelta(days=3), "17:30"),
            provider="Lisbon Walker",
            city="Sintra",
        ),
        res(
            Kind.FERRY,
            "Cais do Sodré → Cacilhas",
            at(d + timedelta(days=4), "10:20"),
            at(d + timedelta(days=4), "10:35"),
            provider="Transtejo",
            start_location="Cais do Sodré",
            end_location="Cacilhas",
            start_tz="Europe/Lisbon",
            end_tz="Europe/Lisbon",
            city="Almada",
        ),
        res(
            Kind.FLIGHT,
            "TP 1351 LIS→LHR",
            at(d + timedelta(days=5), "13:05"),
            at(d + timedelta(days=5), "15:50"),
            provider="TAP Air Portugal",
            confirmation_code="7HQ2LM",
            start_location="LIS",
            end_location="LHR",
            start_tz="Europe/Lisbon",
            end_tz="Europe/London",
            city="London",
            service_number="TP1351",
        ),
    ]
    p = date.today() + timedelta(days=40)
    paris = [
        res(
            Kind.RAIL,
            "Eurostar 9014 London→Paris",
            at(p, "08:01"),
            at(p, "11:20"),
            provider="Eurostar",
            confirmation_code="QJZ8RT",
            start_location="London St Pancras",
            end_location="Paris Gare du Nord",
            start_tz="Europe/London",
            end_tz="Europe/Paris",
            city="Paris",
            seat="Coach 7, seat 61",
        ),
        res(
            Kind.LODGING,
            "Hôtel des Grands Boulevards",
            at(p, "15:00"),
            at(p + timedelta(days=2), "11:00"),
            city="Paris",
            confirmation_code="HGB-5521",
        ),
        res(
            Kind.RAIL,
            "Eurostar 9047 Paris→London",
            at(p + timedelta(days=2), "18:13"),
            at(p + timedelta(days=2), "19:30"),
            provider="Eurostar",
            confirmation_code="QJZ8RT",
            start_location="Paris Gare du Nord",
            end_location="London St Pancras",
            start_tz="Europe/Paris",
            end_tz="Europe/London",
            city="London",
        ),
    ]
    k = date.today() - timedelta(days=75)
    kyoto = [
        res(
            Kind.LODGING,
            "Hotel Kanra Kyoto",
            at(k, "15:00"),
            at(k + timedelta(days=4), "11:00"),
            city="Kyoto",
            start_tz="Asia/Tokyo",
            confirmation_code="KNR-88310",
        ),
        res(
            Kind.RESTAURANT,
            "Gion Karyo",
            at(k + timedelta(days=1), "19:00"),
            city="Kyoto",
            start_tz="Asia/Tokyo",
        ),
    ]

    now = datetime.now(UTC)
    trips: list[str] = []
    for n, (label, rs) in enumerate(
        [
            ("Fwd: Your British Airways booking XK4T9Q", lisbon[:2]),
            ("Casa do Miradouro confirmation", lisbon[2:3]),
            ("Your table at Taberna da Rua das Flores", lisbon[3:4]),
            ("Sintra tour tickets.pdf", lisbon[4:5]),
            ("Transtejo ticket", lisbon[5:6]),
            ("TAP booking 7HQ2LM", lisbon[6:]),
            ("Eurostar e-tickets QJZ8RT", paris),
            ("Kanra Kyoto stay", kyoto),
        ]
    ):
        ingest = f"screenshot-{n}"
        filed = await db.file_reservations(me, ingest, rs)
        trips += [t for t in filed.trip_ids if t not in trips]
        item = InboxItem(
            ingest_id=ingest,
            source="email",
            label=label,
            received_at=now - timedelta(hours=n * 7),
            state="done",
            trip_ids=filed.trip_ids,
        )
        await db.upsert_inbox(me, item)
    unfiled: list[tuple[str, IngestState, str | None]] = [
        ("Fwd: Car hire voucher", "extracting", None),
        ("Screenshot 2026-10-21.png", "queued", None),
        ("Weekly deals from Skyfares", "no_travel", "Not a travel booking"),
        ("scan-0042.pdf", "failed", "The model gave up after 45 minutes of retries"),
    ]
    for n, (label, state, message) in enumerate(unfiled):
        item = InboxItem(
            ingest_id=f"screenshot-pending-{n}",
            source="upload",
            label=label,
            received_at=now - timedelta(minutes=5 + n * 30),
            state=state,
            message=message,
        )
        await db.upsert_inbox(me, item)
    await db.add_notification(
        me,
        Notification(
            id=str(uuid.uuid4()), at=now, trip_id=trips[0], text="Check in for BA 286 opens tomorrow"
        ),
    )
    # A dry run of a better model over the hotel's email: one field changed, a dinner it found too.
    casa = lisbon[2]
    dry = ReprocessRequest(batch_id=BATCH, ingest_id="screenshot-1", mode="dry_run", model="gemma4:31b")
    await db.add_reprocess(dry, me)
    found = casa.model_copy(update={"address": "Rua da Bica de Duarte Belo 47, Lisboa"})
    dinner = lisbon[3].model_copy(update={"id": str(uuid.uuid4())})
    await db.finish_reprocess(
        dry.key, str(uuid.uuid4()), db.reconcile([casa], [found, dinner], [], set()), None
    )
    await seed_photos(skip={"Kyoto"})
    return await auth.create_session(me)


async def seed_photos(skip: set[str]) -> None:
    """Real destination photos, found and stored as the worker does. The past trip goes without, to
    show the placeholder, as does any trip whose photo can't be fetched (Wikimedia unreachable)."""
    for place in await db.places_needing_photos():
        if place in skip:
            continue
        try:
            await place_photo.store(place, await photos.choose(place))
        except httpx.HTTPError as e:
            print(f"no photo for {place}: {e}", file=sys.stderr)


async def serve(app: str, url: str, env: dict[str, str]) -> asyncio.subprocess.Process:
    port = url.rsplit(":", 1)[1]
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "uvicorn",
        app,
        "--host",
        "127.0.0.1",
        "--port",
        port,
        "--log-level",
        "warning",
        env=env,
    )
    async with httpx.AsyncClient() as client:
        for _ in range(100):
            if proc.returncode is not None:
                sys.exit(f"{app} exited with {proc.returncode}")
            try:
                await client.get(url)
                return proc
            except httpx.TransportError:
                await asyncio.sleep(0.1)
    sys.exit(f"{app} didn't start listening on {url}")


async def capture(browser: Browser, out: Path, scheme: str, session: str, trip_id: str) -> None:
    async def context(headers: dict[str, str] | None = None) -> BrowserContext:
        return await browser.new_context(
            viewport=VIEWPORT,
            color_scheme="dark" if scheme == "dark" else "light",
            extra_http_headers=headers,
        )

    # Signed out, then signed in with the seeded user's session cookie.
    anon = await context()
    user = await context()
    await user.add_cookies([{"name": "session", "value": session, "url": WEB}])
    # In production, tailscale serve puts the admin app at /admin on the web app's host and sets the
    # login header; here the admin app is on its own port, so its /static comes from the web app.
    admin = await context({"Tailscale-User-Login": ADMIN_LOGIN})

    async def web_static(route: Route) -> None:
        await route.fulfill(response=await route.fetch(url=WEB + route.request.url.removeprefix(ADMIN)))

    await admin.route(f"{ADMIN}/static/**", web_static)

    shots = [
        (anon, "01-login", f"{WEB}/login"),
        (user, "02-home", f"{WEB}/"),
        (user, "03-trip", f"{WEB}/trips/{trip_id}"),
        (user, "04-account", f"{WEB}/account"),
        (user, "05-account-senders", f"{WEB}/account/senders"),
        (admin, "06-admin", f"{ADMIN}/?batch={BATCH}"),
        (user, "09-account-sharing", f"{WEB}/account/sharing"),
    ]
    (out / scheme).mkdir(parents=True, exist_ok=True)
    for ctx, name, url in shots:
        page = await ctx.new_page()
        # Not networkidle: the home page holds its live-updates stream open.
        resp = await page.goto(url, wait_until="load")
        if resp is None or not resp.ok:
            sys.exit(f"{url} answered {resp and resp.status}")
        await page.evaluate("document.fonts.ready")
        await page.screenshot(path=out / scheme / f"{name}.png", full_page=True, animations="disabled")
        await page.close()
    # The home page again in a short window, where the sidebar scrolls with the page instead of pinning.
    # Viewport shots, top and bottom: a full-page shot would grow the viewport and pin the sidebar again.
    page = await user.new_page()
    await page.set_viewport_size(SHORT)
    await page.goto(f"{WEB}/", wait_until="load")
    await page.evaluate("document.fonts.ready")
    await page.screenshot(path=out / scheme / "07-home-short-top.png", animations="disabled")
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    await page.screenshot(path=out / scheme / "08-home-short-bottom.png", animations="disabled")
    await page.close()
    # A reservation's edit dialog, open over the trip page.
    page = await user.new_page()
    await page.goto(f"{WEB}/trips/{trip_id}", wait_until="load")
    await page.evaluate("document.fonts.ready")
    await page.locator(".timeline button[command=show-modal]:visible").first.click()
    await page.locator("dialog[open]").wait_for()
    await page.screenshot(path=out / scheme / "10-reservation-edit.png", animations="disabled")
    await page.close()
    for ctx in (anon, user, admin):
        await ctx.close()


async def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("out", type=Path, help="where to write light/*.png and dark/*.png")
    args = parser.parse_args()

    url = os.environ.get("DATABASE_URL", settings.database_url)
    load_schema(url)
    db.init(url)
    session = await seed()
    trips = await db.trip_summaries(await db.user_for_email(EMAIL))
    await db.close()

    env = os.environ | {
        "DATABASE_URL": url,
        "PUBLIC_BASE_URL": WEB,
        "ADMIN_TAILSCALE_LOGINS": ADMIN_LOGIN,
        "RESTATE_UI_URL": "http://localhost:9070",
        # Nothing listens here, so the admin page's model status shows its error state at once.
        "OLLAMA_URL": "http://127.0.0.1:9",
        # Only so the account page offers eval set sharing; rendering it never touches a bucket.
        "INPUTS_BUCKET": "triptych-inputs",
        "EVALS_BUCKET": "triptych-evals",
    }
    servers = [
        await serve("triptych.web.app:app", WEB, env),
        await serve("triptych.admin.app:app", ADMIN, env),
    ]
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(executable_path=os.environ.get("PLAYWRIGHT_CHROMIUM") or None)
            # The upcoming trip with the connecting flight.
            upcoming = min((t for t in trips if t.start_date >= date.today()), key=lambda t: t.start_date)
            for scheme in ("light", "dark"):
                await capture(browser, args.out, scheme, session, upcoming.id)
            await browser.close()
    finally:
        for proc in servers:
            proc.terminate()
            await proc.wait()
    print(f"wrote {len(list(args.out.glob('*/*.png')))} screenshots to {args.out}")


if __name__ == "__main__":
    asyncio.run(main())
