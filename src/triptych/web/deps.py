"""What the web app's routers share: the templates, the lifespan's state, who's signed in, rate limits
and trip access, as FastAPI dependencies."""

from pathlib import Path
from typing import Annotated, Any, TypedDict, cast
from zoneinfo import available_timezones

import restate
from fastapi import Depends, HTTPException, Request
from fastapi.responses import Response
from fastapi.templating import Jinja2Templates
from limits import RateLimitItem

from triptych import auth, db, journeys, sharing
from triptych.config import settings
from triptych.models import Kind, Role, Trip, TripAccess
from triptych.web import live, ratelimit
from triptych.web.assets import Assets

HERE = Path(__file__).parent

templates = Jinja2Templates(directory=HERE / "templates")
templates.env.filters["duration"] = journeys.format_duration
templates.env.filters["tz_abbr"] = journeys.tz_abbr
# Jinja types globals by its own defaults' types; any value is allowed.
cast(dict[str, Any], templates.env.globals)["static"] = Assets().url
cast(dict[str, Any], templates.env.globals)["elapsed"] = journeys.elapsed
cast(dict[str, Any], templates.env.globals)["kinds"] = list(Kind)
cast(dict[str, Any], templates.env.globals)["timezones"] = sorted(available_timezones())


class AppState(TypedDict):
    """What the lifespan hands every request (Starlette's lifespan state). Read it with `state()`."""

    restate: restate.RestateClient
    hub: live.Hub
    limiter: ratelimit.Limiter


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
SIGNUP_COOKIE = "__Host-signup" if settings.secure_cookies else "signup"  # see signup.py


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
    if me.waitlisted:
        raise HTTPException(status_code=303, headers={"Location": "/waitlist"})
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


def ics(body: bytes, filename: str) -> Response:
    return Response(
        body, media_type="text/calendar", headers={"Content-Disposition": f'inline; filename="{filename}"'}
    )
