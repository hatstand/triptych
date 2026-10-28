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
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast
from urllib.parse import quote
from zoneinfo import available_timezones

import restate
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent
from fastapi.templating import Jinja2Templates
from limits import RateLimitItem
from pydantic import BaseModel, field_validator, model_validator
from starlette.middleware.base import RequestResponseEndpoint

from triptych import auth, db, enrich, ical, inbound, journeys, llm, senders, sharing, telemetry
from triptych.config import settings
from triptych.models import (
    AddReservation,
    Answer,
    AskTrip,
    EditReservation,
    IngestRequest,
    Kind,
    MergeTrips,
    Notification,
    Overview,
    RemoveReservation,
    RenameTrip,
    ReprocessRequest,
    ReservationFields,
    Role,
    Trip,
    TripAccess,
    TripRef,
)
from triptych.services import assistant as assistant_svc
from triptych.services import eval_share as eval_share_svc
from triptych.services import ingest as ingest_svc
from triptych.services import reprocess as reprocess_svc
from triptych.services import user as user_svc
from triptych.web import csp, live, ratelimit
from triptych.web.assets import STATIC, Assets, Static

HERE = Path(__file__).parent
log = logging.getLogger("triptych.web")
MAX_UPLOAD = 15 * 1024 * 1024
MAX_INBOUND = 25 * 1024 * 1024  # Cloudflare Email Routing's own limit


class AppState(TypedDict):
    """What the lifespan hands every request (Starlette's lifespan state). Read it with `state()`."""

    restate: restate.RestateClient
    hub: live.Hub
    limiter: ratelimit.Limiter


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
templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["duration"] = journeys.format_duration
templates.env.filters["tz_abbr"] = journeys.tz_abbr
# Jinja types globals by its own defaults' types; any value is allowed.
cast(dict[str, Any], templates.env.globals)["static"] = Assets().url
cast(dict[str, Any], templates.env.globals)["elapsed"] = journeys.elapsed
cast(dict[str, Any], templates.env.globals)["kinds"] = list(Kind)
cast(dict[str, Any], templates.env.globals)["timezones"] = sorted(available_timezones())


def state(request: Request) -> AppState:
    # Starlette would type this as Request[AppState].state, but FastAPI only injects a plain
    # Request, so this is the one place that says what the lifespan put there.
    return cast(AppState, request.state)


def rs(request: Request) -> restate.RestateClient:
    return state(request)["restate"]


def live_hub(request: Request) -> live.Hub:
    return state(request)["hub"]


def limiter(request: Request) -> ratelimit.Limiter:
    return state(request)["limiter"]


# __Host- pins the cookie to this exact host over HTTPS (no Domain, Path=/); plain HTTP is local dev.
SESSION_COOKIE = "__Host-session" if settings.secure_cookies else "session"
CEREMONY_COOKIE = "webauthn-ceremony"  # signed ceremony state, see auth.py


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


def set_cookie(resp: Response, name: str, value: str, max_age: int, path: str = "/") -> None:
    resp.set_cookie(
        name, value, max_age=max_age, path=path, httponly=True, secure=settings.secure_cookies, samesite="lax"
    )


async def signed_in(request: Request) -> auth.Me | None:
    token = request.cookies.get(SESSION_COOKIE)
    return await auth.session_user(token) if token else None


async def current_user(request: Request) -> auth.Me:
    me = await signed_in(request)
    if me is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return me


User = Annotated[auth.Me, Depends(current_user)]
Client = Annotated[restate.RestateClient, Depends(rs)]
Limiter = Annotated[ratelimit.Limiter, Depends(limiter)]


def per_client(rate: RateLimitItem, scope: str) -> Any:
    """A route dependency limiting each client address (X-Forwarded-For, via uvicorn)."""

    async def check(request: Request, lim: Limiter) -> None:
        await ratelimit.check(lim, rate, scope, request.client.host if request.client else "unknown")

    return Depends(check)


def per_user(rate: RateLimitItem, scope: str) -> Any:
    """A route dependency limiting each signed-in user, whatever address they come from."""

    async def check(me: User, lim: Limiter) -> None:
        await ratelimit.check(lim, rate, scope, me.id)

    return Depends(check)


def feed_token(user_id: str) -> str:
    return hmac.new(settings.secret_key.encode(), user_id.encode(), hashlib.sha256).hexdigest()[:32]


def trip_access(need: Role) -> Any:
    """The dependency every /trips/{trip_id} route takes: the caller's access to the trip, if it
    allows `need`. Anything less is a 404, the same as no such trip, so ids say nothing."""

    async def check(trip_id: str, me: User) -> TripAccess:
        access = await db.trip_access(me.id, trip_id)
        if access is None or not access.role.allows(need):
            raise HTTPException(404, "trip not found")
        return access

    return Depends(check)


CanView = Annotated[TripAccess, trip_access(Role.VIEW)]
CanEdit = Annotated[TripAccess, trip_access(Role.EDIT)]
Owns = Annotated[TripAccess, trip_access(Role.OWNER)]


async def load_trip(access: TripAccess) -> Trip:
    """The trip as `access` may see it: a viewer's comes without confirmation codes."""
    t = await db.get_trip(access.owner, access.trip_id)
    if t is None:
        raise HTTPException(404, "trip not found")
    return sharing.viewer_view(t) if access.role is Role.VIEW else t


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
    return templates.TemplateResponse(request, "login.html", {})


@app.get(
    "/register/{invite}",
    response_class=HTMLResponse,
    dependencies=[per_client(ratelimit.INVITE_PAGE, "invite-page")],
)
async def register_page(request: Request, invite: str) -> Response:
    email = await auth.invite_email(invite)
    return templates.TemplateResponse(
        request, "register.html", {"invite": invite, "invite_email": email}, status_code=200 if email else 404
    )


@app.post("/logout")
async def logout(request: Request) -> Response:
    if token := request.cookies.get(SESSION_COOKIE):
        await auth.end_session(token)
    resp = RedirectResponse("/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE, path="/", secure=settings.secure_cookies, httponly=True)
    return resp


ACCOUNT_TABS = {"passkeys": "Passkeys", "senders": "Sending addresses", "sharing": "Eval set"}
"""Account page tabs: each is its own URL, /account/<tab>, and /account shows the first."""


def account_tabs() -> dict[str, str]:
    """Eval set sharing is only offered where there's an eval bucket to share into."""
    if settings.evals_bucket and settings.inputs_bucket:
        return ACCOUNT_TABS
    return {k: v for k, v in ACCOUNT_TABS.items() if k != "sharing"}


async def render_account(
    request: Request,
    me: auth.Me,
    tab: str = "passkeys",
    error: str | None = None,
    sender_error: str | None = None,
    new_sender: tuple[str, str] | None = None,
) -> Response:
    """`new_sender` = (address, code) just issued: codes are stored hashed, so shown only now."""
    return templates.TemplateResponse(
        request,
        "account.html",
        {
            "email": me.email,
            "tabs": account_tabs(),
            "tab": tab,
            "passkeys": await auth.passkeys(me.id),
            "senders": await senders.addresses(me.id),
            "pending_senders": await senders.pending(me.id),
            "new_sender": new_sender,
            "inbound_address": settings.inbound_address,
            "sharing": await db.eval_sharing(me.id) if tab == "sharing" else None,
            "error": error,
            "sender_error": sender_error,
        },
        status_code=400 if error or sender_error else 200,
    )


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, me: User) -> Response:
    return await render_account(request, me)


@app.get("/account/{tab}", response_class=HTMLResponse)
async def account_tab(request: Request, tab: str, me: User) -> Response:
    if tab not in account_tabs():
        raise HTTPException(404)
    if tab == next(iter(ACCOUNT_TABS)):
        return RedirectResponse("/account", status_code=308)
    return await render_account(request, me, tab)


@app.post("/account/passkeys/{passkey_id}/delete", response_class=HTMLResponse)
async def passkey_delete(request: Request, passkey_id: str, me: User) -> Response:
    try:
        await auth.delete_passkey(me.id, passkey_id)
    except auth.AuthError as e:
        return await render_account(request, me, error=str(e))
    return RedirectResponse("/account", status_code=303)


@app.post("/account/senders", response_class=HTMLResponse)
async def sender_add(request: Request, me: User, email: Annotated[str, Form()]) -> Response:
    try:
        code = await senders.request(me.id, email)
    except senders.SenderError as e:
        return await render_account(request, me, "senders", sender_error=str(e))
    return await render_account(request, me, "senders", new_sender=(db.normalise_email(email), code))


@app.post("/account/senders/delete", response_class=HTMLResponse)
async def sender_delete(request: Request, me: User, email: Annotated[str, Form()]) -> Response:
    try:
        await senders.remove(me.id, email)
    except senders.SenderError as e:
        return await render_account(request, me, "senders", sender_error=str(e))
    return RedirectResponse("/account/senders", status_code=303)


@app.post("/account/sharing")
async def sharing_set(me: User, client: Client, share: Annotated[bool, Form()]) -> Response:
    """Opting in copies everything already kept, then every new ingest (services/eval_share.py)."""
    if "sharing" not in account_tabs():
        raise HTTPException(404)
    await db.set_eval_sharing(me.id, share)
    if share:
        await client.object_send(eval_share_svc.share_all, me.id, None)
    return RedirectResponse("/account/sharing", status_code=303)


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
    invite: str | None = None


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
    """With an invite: a passkey for the user the invited address belongs to (new or not).
    Without: another passkey for whoever is signed in."""
    if body.invite is not None:
        email = await auth.invite_email(body.invite)
        if email is None:
            raise auth.AuthError("This invite has expired or was already used.")
        user_id = await db.user_for_email(email)
    elif me := await signed_in(request):
        user_id, email = me.id, me.email
    else:
        raise auth.AuthError("Sign in first.")
    return options_response(*await auth.start_registration(user_id, email, body.invite))


@app.post("/auth/register/verify", dependencies=[per_client(ratelimit.REGISTER, "register")])
async def register_verify(request: Request, body: RegisterFinish) -> Response:
    user_id = await auth.finish_registration(request.cookies.get(CEREMONY_COOKIE), body.credential, body.name)
    if (me := await signed_in(request)) and me.id == user_id:
        return signed_in_response("/account")
    return signed_in_response("/", await auth.create_session(user_id))


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


async def trip_context(me: auth.Me, access: TripAccess, t: Trip) -> dict[str, Any]:
    """What trip.html needs to show a trip to someone with `access`."""
    owns = access.role is Role.OWNER
    # Other trips to merge into, where bookings came from and how it's shared are the owner's alone.
    others = [s for s in await db.trip_summaries(me.id) if s.id != t.id] if owns else []
    sources = await db.trip_sources(access.owner, t.id) if owns else []
    links = await sharing.links(access.owner, t.id) if owns else []
    members = await sharing.members(access.owner, t.id) if owns else []
    editors = {r.added_by for r in t.reservations if r.added_by}
    return {
        "email": me.email,
        "role": access.role,
        "trip": t,
        "trip_url": f"/trips/{t.id}",
        "summary": t.summary(),
        "days": journeys.timeline(t.reservations),
        "others": others,
        "sources": sources,
        "links": links,
        "members": members,
        "added_by": {e: await db.primary_email(e) or "someone" for e in editors},
        "shared_by": None if owns else await db.primary_email(access.owner),
        "in_feed": None if owns else await sharing.in_feed(me.id, t.id),
        "base_url": settings.public_base_url.rstrip("/"),
    }


@app.get("/trips/{trip_id}", response_class=HTMLResponse)
async def trip_page(request: Request, me: User, access: CanView) -> Response:
    t = await load_trip(access)
    return templates.TemplateResponse(request, "trip.html", await trip_context(me, access, t))


@app.get("/trips/{trip_id}/photo")
async def trip_photo(trip_id: str, access: CanView) -> Response:
    """The trip's destination photo. Pages link it with the photo's version in the query string, so
    a new photo is a new URL and this one can be cached for good."""
    photo = await db.trip_photo(access.owner, trip_id)
    if photo is None:
        raise HTTPException(404)
    image, content_type = photo
    return Response(
        image, media_type=content_type, headers={"Cache-Control": "private, max-age=31536000, immutable"}
    )


@app.post("/trips/{trip_id}/ask", response_class=HTMLResponse, dependencies=[per_user(ratelimit.ASK, "ask")])
async def trip_ask(
    request: Request, trip_id: str, me: User, access: Owns, client: Client, question: Annotated[str, Form()]
) -> Response:
    t = await load_trip(access)
    answer: Answer = await client.service_call(
        assistant_svc.ask, AskTrip(owner=access.owner, trip_id=trip_id, question=question)
    )
    context = await trip_context(me, access, t)
    return templates.TemplateResponse(
        request, "trip.html", context | {"question": question, "answer": answer.text}
    )


@app.post("/trips/{trip_id}/links", dependencies=[per_user(ratelimit.SHARE_MANAGE, "share")])
async def link_create(
    access: Owns, label: Annotated[str, Form()] = "", expire: Annotated[bool, Form()] = False
) -> Response:
    await sharing.create_link(access.owner, access.trip_id, label[:100], expire_after_trip=expire)
    return RedirectResponse(f"/trips/{access.trip_id}#share", status_code=303)


@app.post("/trips/{trip_id}/links/{link_id}/delete")
async def link_revoke(link_id: str, access: Owns) -> Response:
    await sharing.revoke_link(access.owner, access.trip_id, link_id)
    return RedirectResponse(f"/trips/{access.trip_id}#share", status_code=303)


@app.post("/trips/{trip_id}/members", dependencies=[per_user(ratelimit.SHARE_MANAGE, "share")])
async def member_invite(
    access: Owns, email: Annotated[str, Form()], role: Annotated[Literal["view", "edit"], Form()] = "view"
) -> Response:
    await sharing.invite(access.owner, access.trip_id, email[:320], Role(role))
    return RedirectResponse(f"/trips/{access.trip_id}#share", status_code=303)


@app.post("/trips/{trip_id}/members/remove")
async def member_remove(access: Owns, email: Annotated[str, Form()]) -> Response:
    await sharing.remove_member(access.owner, access.trip_id, email)
    return RedirectResponse(f"/trips/{access.trip_id}#share", status_code=303)


@app.post("/trips/{trip_id}/leave")
async def member_leave(me: User, access: CanView) -> Response:
    await sharing.leave(me.id, access.trip_id)
    return RedirectResponse("/", status_code=303)


@app.post("/trips/{trip_id}/feed")
async def member_feed(me: User, access: CanView, shown: Annotated[bool, Form()]) -> Response:
    """Put a trip shared with you in your calendar feed, or take it out."""
    await sharing.set_in_feed(me.id, access.trip_id, shown)
    return RedirectResponse(f"/trips/{access.trip_id}", status_code=303)


# Invitees have no access to the trip until they accept, so these take no trip_access.
@app.post("/trips/{trip_id}/accept")
async def invite_accept(trip_id: str, me: User) -> Response:
    if not await sharing.accept(me.id, trip_id):
        raise HTTPException(404, "invite not found")
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@app.post("/trips/{trip_id}/decline")
async def invite_decline(trip_id: str, me: User) -> Response:
    await sharing.decline(me.id, trip_id)
    return RedirectResponse("/", status_code=303)


# --- shared links (no sign-in) ---------------------------------------------------------------

LINK_HEADERS = {
    # The token is in the URL: keep it out of Referer headers, search indexes and shared caches.
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "private, no-store",
}


async def linked_trip(token: str) -> sharing.LinkedTrip:
    found = await sharing.open_link(token)
    if found is None:
        raise HTTPException(404, "this link doesn't work any more")
    return found


@app.get(
    "/s/{token}", response_class=HTMLResponse, dependencies=[per_client(ratelimit.SHARE_VIEW, "share-view")]
)
async def shared_page(request: Request, token: str) -> Response:
    found = await linked_trip(token)
    if me := await signed_in(request):
        access = await db.trip_access(me.id, found.trip_id)
        if access is not None:
            return RedirectResponse(f"/trips/{found.trip_id}", status_code=303)
    t = await db.get_trip(found.owner, found.trip_id)
    if t is None:
        raise HTTPException(404)
    shown = sharing.shared_view(t)
    context = {
        "role": None,
        "trip": shown,
        "trip_url": f"/s/{token}",
        "summary": shown.summary(),
        "days": journeys.timeline(shown.reservations),
    }
    return templates.TemplateResponse(request, "trip.html", context, headers=LINK_HEADERS)


@app.get("/s/{token}/calendar.ics", dependencies=[per_client(ratelimit.SHARE_VIEW, "share-view")])
async def shared_ics(token: str) -> Response:
    """The shared trip as a calendar to download or subscribe to, as redacted as its page. It goes
    quiet when the link is revoked or expires."""
    found = await linked_trip(token)
    t = await db.get_trip(found.owner, found.trip_id)
    if t is None:
        raise HTTPException(404)
    resp = ics(ical.calendar([sharing.shared_view(t)], t.name), "trip.ics")
    resp.headers.update(LINK_HEADERS)
    return resp


@app.get("/s/{token}/photo", dependencies=[per_client(ratelimit.SHARE_VIEW, "share-view")])
async def shared_photo(token: str) -> Response:
    found = await linked_trip(token)
    photo = await db.trip_photo(found.owner, found.trip_id)
    if photo is None:
        raise HTTPException(404)
    image, content_type = photo
    # Cached only by this viewer's browser, so a revoked link's photo lingers nowhere else.
    headers = LINK_HEADERS | {"Cache-Control": "private, max-age=86400"}
    return Response(image, media_type=content_type, headers=headers)


@app.post("/trips/{trip_id}/rename")
async def trip_rename(
    trip_id: str, access: CanEdit, client: Client, name: Annotated[str, Form()]
) -> Response:
    await client.object_call(user_svc.rename_trip, access.owner, RenameTrip(trip_id=trip_id, name=name))
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@app.post("/trips/{trip_id}/merge")
async def trip_merge(
    trip_id: str, me: User, access: Owns, client: Client, into: Annotated[str, Form()]
) -> Response:
    target = await db.trip_access(me.id, into)
    if target is None or target.role is not Role.OWNER:
        raise HTTPException(404, "trip not found")
    await client.object_call(user_svc.merge_trips, access.owner, MergeTrips(trip_id=trip_id, into=into))
    return RedirectResponse(f"/trips/{into}", status_code=303)


class ReservationForm(ReservationFields):
    """The trip page's edit form (trip.html): a blank field is none, travelers are one per line."""

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_none(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip() or None
        return v

    @field_validator("travelers", mode="before")
    @classmethod
    def one_per_line(cls, v: Any) -> Any:
        lines = v if isinstance(v, list) else [v or ""]
        return [name.strip() for line in lines for name in str(line).splitlines() if name.strip()]

    @field_validator("start_tz", "end_tz")
    @classmethod
    def known_zone(cls, v: str | None) -> str | None:
        if v is not None and enrich.valid_tz(v) is None:
            raise ValueError(f"{v} isn't a timezone we know, like Europe/Lisbon")
        return v

    @model_validator(mode="after")
    def ends_after_it_starts(self) -> "ReservationForm":
        """The timeline runs forward in time, so nothing may end before it starts. Wall clocks in two
        zones are compared as instants; without both zones, as the times written."""
        if self.end_local is None:
            return self
        start, end = self.start_local, self.end_local
        if self.start_tz and self.end_tz:
            start, end = enrich.to_utc(start, self.start_tz), enrich.to_utc(end, self.end_tz)
        elif self.start_tz or self.end_tz:
            return self  # one zone unknown: it could be either way round
        if start is not None and end is not None and end < start:
            raise ValueError("it ends before it starts")
        return self


class AddReservationForm(ReservationForm):
    ingest_id: str | None = None
    """Which of the trip's emails it was in ("" for none)."""


@app.post("/trips/{trip_id}/reservations")
async def reservation_add(
    trip_id: str, me: User, access: CanEdit, client: Client, form: Annotated[AddReservationForm, Form()]
) -> Response:
    fields = ReservationFields.model_validate(form.model_dump(exclude={"ingest_id"}))
    owns = access.role is Role.OWNER
    req = AddReservation(
        trip_id=trip_id,
        reservation_id=str(uuid.uuid4()),
        # Which email missed it is the owner's to say: the emails are theirs.
        ingest_id=form.ingest_id if owns else None,
        fields=fields,
        added_by=None if owns else me.id,
    )
    await client.object_call(user_svc.add_reservation, access.owner, req)
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@app.post("/trips/{trip_id}/reservations/{reservation_id}/edit")
async def reservation_edit(
    trip_id: str,
    reservation_id: str,
    access: CanEdit,
    client: Client,
    form: Annotated[ReservationForm, Form()],
) -> Response:
    fields = ReservationFields.model_validate(form.model_dump())
    await client.object_call(
        user_svc.edit_reservation,
        access.owner,
        EditReservation(trip_id=trip_id, reservation_id=reservation_id, fields=fields),
    )
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@app.post("/trips/{trip_id}/delete")
async def trip_delete(trip_id: str, access: Owns, client: Client) -> Response:
    await client.object_call(user_svc.delete_trip, access.owner, TripRef(trip_id=trip_id))
    return RedirectResponse("/", status_code=303)


@app.post("/trips/{trip_id}/reservations/{reservation_id}/delete")
async def reservation_delete(trip_id: str, reservation_id: str, access: CanEdit, client: Client) -> Response:
    still_exists = await client.object_call(
        user_svc.remove_reservation,
        access.owner,
        RemoveReservation(trip_id=trip_id, reservation_id=reservation_id),
    )
    return RedirectResponse(f"/trips/{trip_id}" if still_exists else "/", status_code=303)


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
    if code is not None and not await senders.is_pending(code, sender):
        code = None
    owner = await senders.owner(sender)
    if sender != inbound.GMAIL_FORWARDING_VERIFIER and owner is None and code is None:
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

    if code is not None:
        try:
            owner = await senders.confirm(code, result.sender)
        except senders.SenderError as e:
            raise HTTPException(403, str(e)) from e
        text = f"Mail from {result.sender} will now be filed in your account."
        await db.add_notification(owner, Notification(id=str(uuid.uuid4()), at=now, trip_id=None, text=text))
        return {"status": "sender-verified", "user": owner, "auth": result.how}

    assert owner is not None
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


def ics(body: bytes, filename: str) -> Response:
    return Response(
        body, media_type="text/calendar", headers={"Content-Disposition": f'inline; filename="{filename}"'}
    )


@app.get("/trips/{trip_id}/calendar.ics")
async def trip_ics(trip_id: str, access: CanView) -> Response:
    t = await load_trip(access)
    return ics(ical.calendar([t], t.name), f"{trip_id}.ics")


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
