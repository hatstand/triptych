"""Signing in and out with passkeys: /login, /waitlist, /register/{invite}, /logout, and the
/auth WebAuthn ceremonies that also add passkeys and finish signups."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Body, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from pydantic import BaseModel

from triptych import auth, senders, signup
from triptych.config import settings
from triptych.web import ratelimit
from triptych.web.deps import (
    CEREMONY_COOKIE,
    SESSION_COOKIE,
    SIGNUP_COOKIE,
    per_client,
    set_cookie,
    signed_in,
    templates,
)
from triptych.web.signup_pages import signups_open

router = APIRouter()


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Response:
    if await signed_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"signups": settings.signups != "closed"})


@router.get("/waitlist", response_class=HTMLResponse)
async def waitlist_page(request: Request) -> Response:
    """Where a signed-in account still waiting to be let in (signup.py) lands, whatever it asks for."""
    me = await signed_in(request)
    if me is None or not me.waitlisted:
        return RedirectResponse("/" if me else "/login", status_code=303)
    return templates.TemplateResponse(request, "waitlist.html", {"waiting": me.email})


@router.get(
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


@router.post("/logout")
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


@router.post("/auth/login/options", dependencies=[per_client(ratelimit.LOGIN, "login")])
async def login_options() -> Response:
    return options_response(*auth.start_login())


@router.post("/auth/login/verify", dependencies=[per_client(ratelimit.LOGIN, "login")])
async def login_verify(request: Request, credential: Annotated[dict[str, Any], Body()]) -> Response:
    user_id = await auth.finish_login(request.cookies.get(CEREMONY_COOKIE), credential)
    return signed_in_response("/", await auth.create_session(user_id))


@router.post("/auth/register/options", dependencies=[per_client(ratelimit.REGISTER, "register")])
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


@router.post("/auth/register/verify", dependencies=[per_client(ratelimit.REGISTER, "register")])
async def register_verify(request: Request, body: RegisterFinish) -> Response:
    user_id = await auth.finish_registration(request.cookies.get(CEREMONY_COOKIE), body.credential, body.name)
    if (me := await signed_in(request)) and me.id == user_id:
        return signed_in_response("/account")
    resp = signed_in_response("/", await auth.create_session(user_id))
    resp.delete_cookie(SIGNUP_COOKIE, path="/", secure=settings.secure_cookies, httponly=True)
    return resp
