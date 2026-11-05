"""/signup: starting an account by mailing a code from the address it's for (triptych/signup.py)."""

from collections.abc import AsyncIterable
from typing import Annotated, Any
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.sse import EventSourceResponse, ServerSentEvent

from triptych import auth, signup
from triptych.config import settings
from triptych.web import live, ratelimit
from triptych.web.deps import SIGNUP_COOKIE, live_hub, per_client, set_cookie, signed_in, templates

router = APIRouter(prefix="/signup")


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


@router.get("", response_class=HTMLResponse)
async def signup_page(request: Request, s: Signup) -> Response:
    if not signups_open(s):
        raise HTTPException(404)
    if await signed_in(request):
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "signup.html", signup_context(s))


@router.post(
    "",
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


@router.post("/cancel")
async def signup_cancel(request: Request) -> Response:
    if token := request.cookies.get(SIGNUP_COOKIE):
        await signup.cancel(token)
    resp = RedirectResponse("/signup", status_code=303)
    resp.delete_cookie(SIGNUP_COOKIE, path="/", secure=settings.secure_cookies, httponly=True)
    return resp


@router.get("/events", response_class=EventSourceResponse)
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
