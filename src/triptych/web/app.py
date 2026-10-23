"""Web UI + small JSON API. Reads come straight from Postgres; writes go through Restate (the
`User` object serialises a user's trip changes, `Ingest` runs the pipeline)."""

import asyncio
import base64
import hashlib
import hmac
import itertools
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any, cast

import restate
from fastapi import Body, Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.middleware.base import RequestResponseEndpoint

from triptych import auth, db, ical, inbound, llm
from triptych.config import settings
from triptych.models import (
    Answer,
    AskTrip,
    IngestRequest,
    Notification,
    Overview,
    RemoveReservation,
    RenameTrip,
    Reservation,
    Trip,
    TripRef,
)
from triptych.services import assistant as assistant_svc
from triptych.services import ingest as ingest_svc
from triptych.services import user as user_svc

HERE = Path(__file__).parent
MAX_UPLOAD = 15 * 1024 * 1024
MAX_INBOUND = 25 * 1024 * 1024  # Cloudflare Email Routing's own limit


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    db.init()
    async with restate.create_client(settings.restate_ingress_url) as client:
        app.state.restate = client
        yield
    await db.close()


app = FastAPI(title="Triptych", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
templates = Jinja2Templates(directory=HERE / "templates")


def rs(request: Request) -> restate.RestateClient:
    return cast(restate.RestateClient, request.app.state.restate)


# __Host- pins the cookie to this exact host over HTTPS (no Domain, Path=/); plain HTTP is local dev.
SESSION_COOKIE = "__Host-session" if settings.secure_cookies else "session"
CHALLENGE_COOKIE = "webauthn-challenge"


@app.middleware("http")
async def same_origin_writes(request: Request, call_next: RequestResponseEndpoint) -> Response:
    """Browsers send Origin with every POST, so a state change from someone else's page is refused
    here (SameSite=Lax cookies already keep sessions off most of them). /inbound is server-to-server."""
    if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path != "/inbound":
        origin = request.headers.get("origin")
        if origin is not None and origin not in settings.allowed_origins:
            return Response("cross-origin request refused", status_code=403)
    return await call_next(request)


@app.exception_handler(auth.AuthError)
async def auth_error(request: Request, e: auth.AuthError) -> Response:
    return JSONResponse({"error": str(e)}, status_code=400)


def set_cookie(resp: Response, name: str, value: str, max_age: int, path: str = "/") -> None:
    resp.set_cookie(
        name, value, max_age=max_age, path=path, httponly=True, secure=settings.secure_cookies, samesite="lax"
    )


async def signed_in(request: Request) -> str | None:
    token = request.cookies.get(SESSION_COOKIE)
    return await auth.session_user(token) if token else None


async def current_user(request: Request) -> str:
    email = await signed_in(request)
    if email is None:
        raise HTTPException(status_code=303, headers={"Location": "/login"})
    return email


User = Annotated[str, Depends(current_user)]
Client = Annotated[restate.RestateClient, Depends(rs)]


def feed_token(email: str) -> str:
    return hmac.new(settings.secret_key.encode(), email.encode(), hashlib.sha256).hexdigest()[:32]


async def load_trip(email: str, trip_id: str) -> Trip:
    t = await db.get_trip(email, trip_id)
    if t is None:
        raise HTTPException(404, "trip not found")
    return t


async def overview(email: str) -> Overview:
    trips, inbox, notifications = await asyncio.gather(
        db.trip_summaries(email), db.inbox(email), db.notifications(email)
    )
    return Overview(trips=trips, inbox=inbox, notifications=notifications)


def by_day(rs_: list[Reservation]) -> list[tuple[date, list[Reservation]]]:
    return [(d, list(g)) for d, g in itertools.groupby(rs_, key=lambda r: r.start_date)]


# --- pages -----------------------------------------------------------------------------------


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if await signed_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {})


@app.get("/register/{invite}", response_class=HTMLResponse)
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


async def render_account(request: Request, email: str, error: str | None = None) -> Response:
    return templates.TemplateResponse(
        request,
        "account.html",
        {"email": email, "passkeys": await auth.passkeys(email), "error": error},
        status_code=400 if error else 200,
    )


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request, email: User) -> Response:
    return await render_account(request, email)


@app.post("/account/passkeys/{passkey_id}/delete", response_class=HTMLResponse)
async def passkey_delete(request: Request, passkey_id: str, email: User) -> Response:
    try:
        await auth.delete_passkey(email, passkey_id)
    except auth.AuthError as e:
        return await render_account(request, email, str(e))
    return RedirectResponse("/account", status_code=303)


# --- passkey ceremonies (driven by static/passkey.js) ----------------------------------------


def options_response(challenge_id: str, options_json: str) -> Response:
    resp = Response(options_json, media_type="application/json")
    set_cookie(resp, CHALLENGE_COOKIE, challenge_id, int(auth.CHALLENGE_TTL.total_seconds()), path="/auth")
    return resp


def signed_in_response(redirect: str, session_token: str | None = None) -> Response:
    resp = JSONResponse({"redirect": redirect})
    resp.delete_cookie(CHALLENGE_COOKIE, path="/auth")
    if session_token is not None:
        set_cookie(resp, SESSION_COOKIE, session_token, int(auth.SESSION_TTL.total_seconds()))
    return resp


class RegisterStart(BaseModel):
    invite: str | None = None


class RegisterFinish(BaseModel):
    credential: dict[str, Any]
    name: str = ""


@app.post("/auth/login/options")
async def login_options() -> Response:
    return options_response(*await auth.start_login())


@app.post("/auth/login/verify")
async def login_verify(request: Request, credential: Annotated[dict[str, Any], Body()]) -> Response:
    email = await auth.finish_login(request.cookies.get(CHALLENGE_COOKIE), credential)
    return signed_in_response("/", await auth.create_session(email))


@app.post("/auth/register/options")
async def register_options(request: Request, body: RegisterStart) -> Response:
    """With an invite: the first passkey for the invited address. Without: another passkey for
    whoever is signed in."""
    if body.invite is not None:
        email = await auth.invite_email(body.invite)
        if email is None:
            raise auth.AuthError("This invite has expired or was already used.")
    elif (email := await signed_in(request)) is None:
        raise auth.AuthError("Sign in first.")
    return options_response(*await auth.start_registration(email, body.invite))


@app.post("/auth/register/verify")
async def register_verify(request: Request, body: RegisterFinish) -> Response:
    email = await auth.finish_registration(request.cookies.get(CHALLENGE_COOKIE), body.credential, body.name)
    if await signed_in(request) == email:
        return signed_in_response("/account")
    return signed_in_response("/", await auth.create_session(email))


@app.get("/", response_class=HTMLResponse)
async def home(request: Request, email: User) -> Response:
    ov = await overview(email)
    today = date.today()
    busy = any(i.state in ("queued", "parsing", "extracting", "filing") for i in ov.inbox)
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "email": email,
            "upcoming": [t for t in ov.trips if t.end_date >= today],
            "past": [t for t in reversed(ov.trips) if t.end_date < today],
            "inbox": ov.inbox[:10],
            "notifications": ov.notifications,
            "busy": busy,
            "feed_url": f"{settings.public_base_url}/feed/{feed_token(email)}.ics?u={email}",
            "today": today,
        },
    )


@app.post("/plans")
async def add_plans(
    email: User,
    client: Client,
    text: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> Response:
    if file is not None and file.filename:
        data = await file.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "file too large")
        req = IngestRequest(
            user=email,
            source="upload",
            filename=file.filename,
            content_type=file.content_type or "application/octet-stream",
            data_b64=base64.b64encode(data).decode(),
            received_at=datetime.now(UTC),
        )
    elif text.strip():
        req = IngestRequest(
            user=email,
            source="paste",
            subject=text.strip().splitlines()[0][:80],
            data_b64=base64.b64encode(text.encode()).decode(),
            received_at=datetime.now(UTC),
        )
    else:
        return RedirectResponse("/", status_code=303)
    await client.workflow_send(ingest_svc.run, f"web-{uuid.uuid4()}", req)
    return RedirectResponse("/", status_code=303)


@app.get("/trips/{trip_id}", response_class=HTMLResponse)
async def trip_page(request: Request, trip_id: str, email: User) -> Response:
    t = await load_trip(email, trip_id)
    return templates.TemplateResponse(
        request,
        "trip.html",
        {"email": email, "trip": t, "summary": t.summary(), "days": by_day(t.reservations)},
    )


@app.post("/trips/{trip_id}/ask", response_class=HTMLResponse)
async def trip_ask(
    request: Request, trip_id: str, email: User, client: Client, question: Annotated[str, Form()]
) -> Response:
    t = await load_trip(email, trip_id)
    answer: Answer = await client.service_call(
        assistant_svc.ask, AskTrip(owner=email, trip_id=trip_id, question=question)
    )
    return templates.TemplateResponse(
        request,
        "trip.html",
        {
            "email": email,
            "trip": t,
            "summary": t.summary(),
            "days": by_day(t.reservations),
            "question": question,
            "answer": answer.text,
        },
    )


@app.post("/trips/{trip_id}/rename")
async def trip_rename(trip_id: str, email: User, client: Client, name: Annotated[str, Form()]) -> Response:
    await client.object_call(user_svc.rename_trip, email, RenameTrip(trip_id=trip_id, name=name))
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@app.post("/trips/{trip_id}/delete")
async def trip_delete(trip_id: str, email: User, client: Client) -> Response:
    await client.object_call(user_svc.delete_trip, email, TripRef(trip_id=trip_id))
    return RedirectResponse("/", status_code=303)


@app.post("/trips/{trip_id}/reservations/{reservation_id}/delete")
async def reservation_delete(trip_id: str, reservation_id: str, email: User, client: Client) -> Response:
    still_exists = await client.object_call(
        user_svc.remove_reservation, email, RemoveReservation(trip_id=trip_id, reservation_id=reservation_id)
    )
    return RedirectResponse(f"/trips/{trip_id}" if still_exists else "/", status_code=303)


@app.post("/notifications/dismiss")
async def notifications_dismiss(email: User) -> Response:
    await db.dismiss_notifications(email)
    return RedirectResponse("/", status_code=303)


# --- inbound mail (Cloudflare Email Worker → Tailscale Funnel → here) -------------------------


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

    envelope_from = request.headers.get("x-envelope-from", "")
    try:
        result = await asyncio.to_thread(inbound.verify, raw, envelope_from, settings.inbound_allowed_senders)
    except inbound.Rejected as e:
        raise HTTPException(403, str(e)) from e

    now = datetime.now(UTC)
    if isinstance(result, inbound.ForwardingConfirmation):
        text = "Gmail forwarding confirmation"
        text += f" — code {result.code}" if result.code else ""
        text += f" — confirm at {result.link}" if result.link else ""
        await db.add_notification(
            result.user, Notification(id=str(uuid.uuid4()), at=now, trip_id=None, text=text)
        )
        return {"status": "forwarding-confirmation", "user": result.user}

    req = IngestRequest(
        user=result.user,
        source="email",
        content_type="message/rfc822",
        data_b64=base64.b64encode(raw).decode(),
        received_at=now,
    )
    key = inbound.ingest_id_for(raw, result.user)
    await client.workflow_send(ingest_svc.run, key, req)
    return {"status": "queued", "ingest_id": key, "user": result.user, "auth": result.how}


# --- calendar feeds --------------------------------------------------------------------------


def ics(body: bytes, filename: str) -> Response:
    return Response(
        body, media_type="text/calendar", headers={"Content-Disposition": f'inline; filename="{filename}"'}
    )


@app.get("/trips/{trip_id}/calendar.ics")
async def trip_ics(trip_id: str, email: User) -> Response:
    t = await load_trip(email, trip_id)
    return ics(ical.calendar([t], t.name), f"{trip_id}.ics")


@app.get("/feed/{token}.ics")
async def feed(token: str, u: str) -> Response:
    if not hmac.compare_digest(token, feed_token(u)):
        raise HTTPException(404)
    return ics(ical.calendar(await db.all_trips(u), "Triptych"), "triptych.ics")


# --- JSON API --------------------------------------------------------------------------------


@app.get("/api/overview")
async def api_overview(email: User) -> Overview:
    return await overview(email)


@app.get("/api/trips/{trip_id}")
async def api_trip(trip_id: str, email: User) -> Trip:
    return await load_trip(email, trip_id)


@app.get("/healthz")
async def healthz() -> dict[str, Any]:
    try:
        return {"ok": True, "llm": await llm.health()}
    except Exception as e:
        return {"ok": False, "llm_error": str(e)}
