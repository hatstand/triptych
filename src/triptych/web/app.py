"""Web UI + small JSON API. Reads come straight from Postgres; writes go through Restate (the
`User` object serialises a user's trip changes, `Ingest` runs the pipeline)."""

import asyncio
import base64
import hashlib
import hmac
import logging
import uuid
from collections.abc import AsyncGenerator, AsyncIterable
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from typing import Annotated, Any
from urllib.parse import quote, urlencode

import restate
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint

from triptych import (
    auth,
    db,
    ical,
    inbound,
    llm,
    senders,
    sharing,
    signup,
    telemetry,
)
from triptych.config import settings
from triptych.models import (
    IngestRequest,
    Notification,
    Overview,
    ReprocessRequest,
    Role,
    Trip,
)
from triptych.services import ingest as ingest_svc
from triptych.services import reprocess as reprocess_svc
from triptych.web import account, csp, links, live, ratelimit, trips
from triptych.web.assets import STATIC, Static
from triptych.web.deps import (
    CEREMONY_COOKIE,
    SESSION_COOKIE,
    SIGNUP_COOKIE,
    AppState,
    CanView,
    Client,
    User,
    ics,
    live_hub,
    load_trip,
    per_client,
    per_user,
    set_cookie,
    signed_in,
    templates,
)

log = logging.getLogger("triptych.web")
MAX_UPLOAD = 15 * 1024 * 1024
MAX_INBOUND = 25 * 1024 * 1024  # Cloudflare Email Routing's own limit


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[AppState]:
    telemetry.setup("triptych-web")
    db.init()
    hub = live.Hub(settings.database_url)
    changes = asyncio.create_task(hub.run())
    async with restate.create_client(settings.restate_ingress_url) as client:
        yield {"restate": client, "hub": hub, "limiter": ratelimit.limiter()}
    changes.cancel()
    await db.close()


# No /docs or /openapi.json: the UI is public, and the JSON API is ours, not a published one.
app = FastAPI(title="Triptych", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Built by the frontend (gitignored), so absent until then; templates still render without it.
app.mount("/static", Static(directory=STATIC, check_dir=False), name="static")
app.middleware("http")(csp.content_security_policy)
app.include_router(trips.router)
app.include_router(links.router)
app.include_router(account.router)


@app.middleware("http")
async def same_origin_writes(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Browsers send Origin with every POST, so a state change from someone else's page is refused
    here (SameSite=Lax cookies already keep sessions off most of them). /inbound is server-to-server."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path != "/inbound":
        origin = request.headers.get("origin")
        if origin is not None and origin not in settings.allowed_origins:
            # Usually PUBLIC_BASE_URL not matching the address bar (http vs https, a port, another
            # hostname), so say what was expected.
            allowed = ", ".join(settings.allowed_origins)
            log.warning(
                "refused %s %s from origin %s (allowed: %s)",
                request.method,
                request.url.path,
                origin,
                allowed,
            )
            msg = f"Requests from {origin} are refused; this server expects {allowed} (PUBLIC_BASE_URL)."
            return JSONResponse(
                {"error": msg},
                status_code=403,
            )
    return await call_next(request)


# A year, the usual floor. No includeSubDomains: nothing is served below these hostnames, and the
# header can only reach down, never up to purplehatstands.com. No preload, which needs both.
HSTS = "max-age=31536000"


@app.middleware("http")
async def strict_transport(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Tells browsers to only ever use HTTPS for this host, whichever one they came in on (the public
    name or the tailnet one). TLS ends before us, so whether it's on comes from PUBLIC_BASE_URL;
    plain-HTTP local dev gets none (browsers ignore it over HTTP anyway)."""
    resp = await call_next(request)
    if settings.secure_cookies:
        resp.headers.setdefault("strict-transport-security", HSTS)
    return resp


@app.exception_handler(auth.AuthError)
async def auth_error(request: Request, e: auth.AuthError) -> Response:
    return JSONResponse({"error": str(e)}, status_code=400)


def feed_token(user_id: str) -> str:
    return hmac.new(settings.secret_key.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:32]


async def overview(owner: str) -> Overview:
    trips, inbox, notifications = await asyncio.gather(
        db.trip_summaries(owner), db.inbox(owner), db.notifications(owner)
    )
    return Overview(trips=trips, inbox=inbox, notifications=notifications)


# --- pages -----------------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if await signed_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"signups": settings.signups != "closed"})


@app.get("/waitlist", response_class=HTMLResponse)
async def waitlist_page(request: Request) -> Response:
    """Where a signed-in account still waiting to be let in (signup.py) lands, whatever it asks for."""
    me = await signed_in(request)
    if me is None or not me.waitlisted:
        return RedirectResponse("/" if me else "/login", status_code=303)
    return templates.TemplateResponse(request, "waitlist.html", {"waiting": me.email})


@app.get(
    "/register/{invite}",
    response_class=HTMLResponse,
    dependencies=[per_client(ratelimit.INVITE_PAGE, "invite-page")],
)
async def register_page(request: Request, invite: str) -> Response:
    """An admin invite link: a button that starts a signup for the address it names (signup.py)."""
    email = await auth.invite_email(invite)
    return templates.TemplateResponse(
        request, "register.html", {"invite": invite, "invite_email": email}, status_code=200 if email else 404
    )


# --- signing up (signup.py) -------------------------------------------------------------------


async def current_signup(request: Request) -> signup.Signup | None:
    token = request.cookies.get(SIGNUP_COOKIE)
    return await signup.get(token) if token else None


Signup = Annotated[signup.Signup | None, Depends(current_signup)]


async def live_signup(s: Signup) -> signup.Signup:
    if s is None:
        raise HTTPException(404)
    return s


def compose_links(s: signup.Signup) -> dict[str, str]:
    """The same message as a mailto: link and as Gmail's and Outlook's web compose pages, for people
    whose browser has no mail app to hand a mailto: link to."""
    to, subject = settings.inbound_address, f"Sign me up to Triptych ({s.code})"
    body = f"Please sign {s.email} up to Triptych. My code is {s.code}."
    return {
        "mailto": f"mailto:{to}?" + urlencode({"subject": subject, "body": body}, quote_via=quote),
        "gmail": "https://mail.google.com/mail/?"
        + urlencode({"view": "cm", "fs": "1", "to": to, "su": subject, "body": body}),
        "outlook": "https://outlook.live.com/mail/0/deeplink/compose?"
        + urlencode({"to": to, "subject": subject, "body": body}),
    }


def signups_open(s: signup.Signup | None = None) -> bool:
    """Whether signing up is on, or this signup came from an admin invite, which works regardless."""
    return settings.signups != "closed" or (s is not None and s.invited)


def signup_context(s: signup.Signup | None) -> dict[str, Any]:
    return {
        "signup": s,
        "links": compose_links(s) if s else None,
        "inbound_address": settings.inbound_address,
    }


@app.get("/signup", response_class=HTMLResponse)
async def signup_page(request: Request, s: Signup) -> Response:
    if not signups_open(s):
        raise HTTPException(404)
    if await signed_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "signup.html", signup_context(s))


@app.post(
    "/signup",
    response_class=HTMLResponse,
    dependencies=[per_client(ratelimit.SIGNUP, "signup")],
)
async def signup_start(
    request: Request, email: Annotated[str, Form()] = "", invite: Annotated[str, Form()] = ""
) -> Response:
    """Start signing up as the address typed in, or the one an admin invite names."""
    if invite:
        invited = await auth.invite_email(invite)
        if invited is None:
            return templates.TemplateResponse(request, "register.html", {}, status_code=404)
        email = invited
    elif not signups_open():
        raise HTTPException(404)
    try:
        token = await signup.start(email, auth.invite_hash(invite) if invite else None)
    except signup.SignupError as e:
        ctx = signup_context(None) | {"error": str(e), "entered": email}
        return templates.TemplateResponse(request, "signup.html", ctx, status_code=400)
    resp = RedirectResponse("/signup", status_code=303)
    set_cookie(resp, SIGNUP_COOKIE, token, int(signup.TTL.total_seconds()))
    return resp


@app.post("/signup/cancel")
async def signup_cancel(request: Request) -> Response:
    if token := request.cookies.get(SIGNUP_COOKIE):
        await signup.cancel(token)
    resp = RedirectResponse("/signup", status_code=303)
    resp.delete_cookie(SIGNUP_COOKIE, path="/", secure=settings.secure_cookies, httponly=True)
    return resp


@app.get("/signup/events", response_class=EventSourceResponse)
async def signup_events(
    s: Annotated[signup.Signup, Depends(live_signup)],
    hub: Annotated[live.Hub, Depends(live_hub)],
    request: Request,
) -> AsyncIterable[ServerSentEvent]:
    """The signup page's step, re-sent when mail proves the address (see live.py, signup.html)."""
    token = request.cookies.get(SIGNUP_COOKIE, "")

    async def render() -> dict[str, str]:
        ctx = signup_context(await signup.get(token))
        return {"signup": templates.get_template("_signup.html").render(ctx)}

    async for e in live.stream(hub, s.channel, render):
        yield e


@app.post("/logout")
async def logout(request: Request) -> Response:
    if token := request.cookies.get(SESSION_COOKIE):
        await auth.end_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/", secure=settings.secure_cookies, httponly=True)
    return resp


# --- passkey ceremonies (driven by static/passkey.js) ----------------------------------------


def options_response(state: str, options_json: str) -> Response:
    resp = Response(options_json, media_type="application/json")
    set_cookie(resp, CEREMONY_COOKIE, state, int(auth.CHALLENGE_TTL.total_seconds()), path="/auth")
    return resp


def signed_in_response(redirect: str, session_token: str | None = None) -> Response:
    resp = JSONResponse({"redirect": redirect})
    resp.delete_cookie(CEREMONY_COOKIE, path="/auth")
    if session_token is not None:
        set_cookie(resp, SESSION_COOKIE, session_token, int(auth.SESSION_TTL.total_seconds()))
    return resp


class RegisterStart(BaseModel):
    signup: bool = False  # create the account a verified signup (its cookie) asked for


class RegisterFinish(BaseModel):
    credential: dict[str, Any]
    name: str = ""


@app.post("/auth/login/options", dependencies=[per_client(ratelimit.LOGIN, "login")])
async def login_options() -> Response:
    return options_response(*auth.start_login())


@app.post("/auth/login/verify", dependencies=[per_client(ratelimit.LOGIN, "login")])
async def login_verify(request: Request, credential: Annotated[dict[str, Any], Body()]) -> Response:
    user_id = await auth.finish_login(request.cookies.get(CEREMONY_COOKIE), credential)
    return signed_in_response("/", await auth.create_session(user_id))


@app.post("/auth/register/options", dependencies=[per_client(ratelimit.REGISTER, "register")])
async def register_options(request: Request, body: RegisterStart) -> Response:
    """With a verified signup: the first passkey of the account it creates (or, invited, of the
    address's existing account). Otherwise: another passkey for whoever is signed in."""
    if body.signup:
        token = request.cookies.get(SIGNUP_COOKIE, "")
        s = await signup.get(token) if token else None
        if s is None or not s.verified or not signups_open(s):
            raise auth.AuthError("This signup has expired. Please start again.")
        user_id = await senders.owner(s.email) if s.taken else str(uuid.uuid4())
        if user_id is None or (s.taken and not s.invited):
            raise auth.AuthError(f"{s.email} already has an account. Sign in instead.")
        return options_response(*await auth.start_registration(user_id, s.email, signup_token=token))
    if (me := await signed_in(request)) is None:
        raise auth.AuthError("Sign in first.")
    return options_response(*await auth.start_registration(me.id, me.email))


@app.post("/auth/register/verify", dependencies=[per_client(ratelimit.REGISTER, "register")])
async def register_verify(request: Request, body: RegisterFinish) -> Response:
    user_id = await auth.finish_registration(request.cookies.get(CEREMONY_COOKIE), body.credential, body.name)
    if (me := await signed_in(request)) and me.id == user_id:
        return signed_in_response("/account")
    resp = signed_in_response("/", await auth.create_session(user_id))
    resp.delete_cookie(SIGNUP_COOKIE, path="/", secure=settings.secure_cookies, httponly=True)
    return resp


def google_calendar_link(feed_url: str) -> str:
    """Google Calendar's "add by URL" page, pre-filled with the feed. Google wants it as webcal://."""
    webcal = feed_url.replace("https://", "webcal://", 1).replace("http://", "webcal://", 1)
    return f"https://calendar.google.com/calendar/render?cid={quote(webcal, safe='')}"


async def home_context(me: auth.Me) -> dict[str, Any]:
    ov, shared, invites = await asyncio.gather(
        overview(me.id), db.shared_trip_summaries(me.id), sharing.pending_invites(me.id)
    )
    today = date.today()
    feed_url = f"{settings.public_base_url}/feed/{feed_token(me.id)}.ics?u={me.id}"
    return {
        "email": me.email,
        "inbound_address": settings.inbound_address,
        "upcoming": [t for t in ov.trips if t.end_date >= today],
        "past": [t for t in reversed(ov.trips) if t.end_date < today],
        "shared": shared,
        "invites": invites,
        "inbox": ov.inbox[:10],
        "notifications": ov.notifications,
        "feed_url": feed_url,
        "google_calendar_url": google_calendar_link(feed_url),
        "today": today,
    }


# The home page's live sections, by SSE event name (sse-swap in index.html).
HOME_SECTIONS = {"alerts": "_alerts.html", "trips": "_trips.html", "inbox": "_inbox.html"}


async def home_sections(me: auth.Me) -> dict[str, str]:
    ctx = await home_context(me)
    return {name: templates.get_template(t).render(ctx) for name, t in HOME_SECTIONS.items()}


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, me: User) -> Response:
    return templates.TemplateResponse(request, "index.html", await home_context(me))


@app.get("/events", response_class=EventSourceResponse)
async def events(me: User, hub: Annotated[live.Hub, Depends(live_hub)]) -> AsyncIterable[ServerSentEvent]:
    """The home page's live sections, re-sent whenever they change (see live.py, index.html)."""
    async for e in live.stream(hub, me.id, lambda: home_sections(me)):
        yield e


@app.post("/plans", dependencies=[per_user(ratelimit.ADD_PLANS, "plans")])
async def add_plans(
    me: User,
    client: Client,
    text: Annotated[str, Form()] = "",
    file: Annotated[list[UploadFile] | None, File()] = None,
) -> Response:
    """The "Add plans" form, and the page-wide drop zone (which posts every dropped file at once).
    Each file becomes its own ingest, exactly like one forwarded email."""
    now = datetime.now(UTC)
    reqs: list[IngestRequest] = []
    for f in file or []:
        if not f.filename:
            continue
        data = await f.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, f"{f.filename} is too large")
        reqs.append(
            IngestRequest(
                user=me.id,
                source="upload",
                filename=f.filename,
                content_type=f.content_type or "application/octet-stream",
                data_b64=base64.b64encode(data).decode(),
                received_at=now,
            )
        )
    if not reqs and text.strip():
        reqs.append(
            IngestRequest(
                user=me.id,
                source="paste",
                subject=text.strip().splitlines()[0][:80],
                data_b64=base64.b64encode(text.encode()).decode(),
                received_at=now,
            )
        )
    for req in reqs:
        key = f"web-{uuid.uuid4()}"
        await client.workflow_send(ingest_svc.run, key, req)
        # Show it in the inbox now rather than when the workflow first runs.
        await db.add_inbox_item(me.id, ingest_svc.queued_item(key, req))
    return RedirectResponse("/", status_code=303)


@app.post("/inbox/{ingest_id}/booking")
async def inbox_booking(ingest_id: str, me: User, client: Client) -> Response:
    """The owner says a "no travel" ingest is a booking: read it again, telling the model so."""
    if not await db.mark_reclassified(me.id, ingest_id):
        raise HTTPException(404)
    req = ReprocessRequest(
        batch_id=reprocess_svc.OWNER_BATCH, ingest_id=ingest_id, mode="apply", model=settings.llm_model,
        owner_says_travel=True,
    )  # fmt: skip
    await db.add_reprocess(req, me.id)
    await client.workflow_send(reprocess_svc.run, req.key, req)
    return RedirectResponse("/", status_code=303)


@app.post("/notifications/dismiss")
async def notifications_dismiss(me: User) -> Response:
    await db.dismiss_notifications(me.id)
    return RedirectResponse("/", status_code=303)


# --- inbound mail (Cloudflare Email Worker → here) -------------------------------------------


@app.post("/inbound")
async def inbound_mail(request: Request, client: Client) -> dict[str, str]:
    """Raw RFC 822 body. 403 = permanent reject (the Worker bounces it to the sender with our
    reason); 5xx/network errors = the Worker retries, then falls back to forwarding to a mailbox."""
    expected = settings.inbound_token
    supplied = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not expected or not hmac.compare_digest(supplied, expected):
        raise HTTPException(404)
    raw = await request.body()
    if len(raw) > MAX_INBOUND:
        raise HTTPException(413, "message too large")

    # Who may file is checked before authenticating, so mail from strangers costs no DNS lookups.
    sender = inbound.canonical_sender(request.headers.get("x-envelope-from", ""))
    code = inbound.verification_code(raw)
    signup_code = None
    if code is not None and not await senders.is_pending(code, sender):
        signup_code = code if await signup.is_pending(code, sender) else None
        code = None
    owner = await senders.owner(sender)
    if sender != inbound.GMAIL_FORWARDING_VERIFIER and owner is None and code is None and signup_code is None:
        raise HTTPException(
            403,
            f"{sender or 'this address'} isn't a Triptych sender. Add it on your Triptych account page,"
            f" or forward from an address you have already added.",
        )
    try:
        result = await asyncio.to_thread(inbound.verify, raw, sender)
    except inbound.Rejected as e:
        raise HTTPException(403, str(e)) from e

    now = datetime.now(UTC)
    if isinstance(result, inbound.ForwardingConfirmation):
        owners = [o for a in result.addresses if (o := await senders.owner(a))]
        if not owners:
            raise HTTPException(403, "forwarding was requested by an address that isn't a Triptych sender")
        text = "Gmail forwarding confirmation"
        text += f" — code {result.code}" if result.code else ""
        text += f" — confirm at {result.link}" if result.link else ""
        await db.add_notification(
            owners[0], Notification(id=str(uuid.uuid4()), at=now, trip_id=None, text=text)
        )
        return {"status": "forwarding-confirmation", "user": owners[0]}

    if (code is not None or signup_code is not None) and not result.how.startswith("dkim:"):
        # An ARC seal says the mail passed through Gmail, not whose mailbox it left from.
        raise HTTPException(
            403, f"send the verification code from {result.sender} directly, not by auto-forward"
        )

    if signup_code is not None:
        try:
            await signup.confirm(signup_code, result.sender)
        except signup.SignupError as e:
            raise HTTPException(403, str(e)) from e
        return {"status": "signup-verified", "auth": result.how}

    if code is not None:
        try:
            owner = await senders.confirm(code, result.sender)
        except senders.SenderError as e:
            raise HTTPException(403, str(e)) from e
        text = f"Mail from {result.sender} will now be filed in your account."
        await db.add_notification(owner, Notification(id=str(uuid.uuid4()), at=now, trip_id=None, text=text))
        return {"status": "sender-verified", "user": owner, "auth": result.how}

    assert owner is not None
    if await signup.is_waitlisted(owner):
        raise HTTPException(
            403, "your Triptych account is still on the waitlist; try again once you're let in"
        )
    if result.how.startswith("dkim:"):
        await senders.mark_verified(result.sender)
    req = IngestRequest(
        user=owner,
        source="email",
        content_type="message/rfc822",
        data_b64=base64.b64encode(raw).decode(),
        received_at=now,
    )
    key = inbound.ingest_id_for(raw, result.sender)
    await client.workflow_send(ingest_svc.run, key, req)
    return {"status": "queued", "ingest_id": key, "user": owner, "auth": result.how}


# --- calendar feeds --------------------------------------------------------------------------


@app.get("/feed/{token}.ics")
async def feed(token: str, u: str) -> Response:
    if not hmac.compare_digest(token, feed_token(u)):
        raise HTTPException(404)
    trips = [sharing.viewer_view(t) if role is Role.VIEW else t for t, role in await db.feed_trips(u)]
    return ics(ical.calendar(trips, "Triptych"), "triptych.ics")


# --- JSON API --------------------------------------------------------------------------------


@app.get("/api/overview")
async def api_overview(me: User) -> Overview:
    return await overview(me.id)


@app.get("/api/trips/{trip_id}")
async def api_trip(trip_id: str, access: CanView) -> Trip:
    return await load_trip(access)


@app.get("/livez")
async def livez() -> dict[str, bool]:
    """The container healthcheck: the app is up and serving. Cheap, unlike /healthz (no LLM call)."""
    return {"ok": True}


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    try:
        return {"ok": True, "llm": await llm.health()}
    except Exception as e:
        return {"ok": False, "llm_error": str(e)}
