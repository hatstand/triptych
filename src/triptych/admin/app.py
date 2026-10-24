"""Admin pages, authorised by Tailscale identity instead of the app's own sign-in.

This is a separate ASGI app that listens only on a Unix socket (compose service `admin`), and
`tailscale serve` on marvin proxies https://triptych.<tailnet>.ts.net/admin to that socket. Serve
deletes any client-supplied Tailscale-User-* headers and sets Tailscale-User-Login itself, and only
for tailnet peers owned by a user (tagged devices and Funnel traffic get none). Nothing else can
reach the socket: it is not a TCP port, so no Funnel route, compose neighbour or future public
proxy pointed at the web app can get a forged header in here. The web app never reads the header.

Tailscale identity is ambient authority, like a cookie: a page in another tab on a tailnet device
can post a form here and Serve will vouch for it. So every write also needs a matching Origin.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, cast, get_args

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

from triptych import auth, db, llm, telemetry
from triptych.config import settings
from triptych.models import IngestState
from triptych.web.assets import Assets

HERE = Path(__file__).parent
LOGIN_HEADER = "Tailscale-User-Login"
STATES: tuple[str, ...] = get_args(IngestState)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    with telemetry.running("triptych-admin"):
        db.init()
        yield
        await db.close()


app = FastAPI(title="Triptych admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
FastAPIInstrumentor.instrument_app(app, exclude_spans=["receive", "send"])
# Admin templates extend the web app's base.html; /static is served by the web app on the same host.
templates = Jinja2Templates(directory=[HERE / "templates", HERE.parent / "web" / "templates"])
# Jinja types globals by its own defaults' types; any value is allowed.
cast(dict[str, Any], templates.env.globals)["static"] = Assets().url


def tailscale_admin(request: Request) -> str:
    login = request.headers.get(LOGIN_HEADER, "").strip().lower()
    if not login:
        raise HTTPException(403, "no Tailscale identity: open this from a device signed in to the tailnet")
    if login not in settings.admin_logins:
        raise HTTPException(403, f"{login} is not in ADMIN_TAILSCALE_LOGINS")
    return login


Admin = Annotated[str, Depends(tailscale_admin)]


def same_origin(request: Request) -> None:
    """Browsers send Origin with every form POST; a missing one means not a browser form of ours."""
    origin = request.headers.get("origin")
    if origin not in settings.admin_origins:
        raise HTTPException(403, f"refusing a write from origin {origin or '(none)'}")


SameOrigin = Depends(same_origin)


async def render(request: Request, login: str, state: str | None = None, **extra: Any) -> Response:
    if state is not None and state not in STATES:
        raise HTTPException(400, f"state must be one of {', '.join(STATES)}")
    counts = await db.ingest_counts(datetime.now(UTC) - timedelta(days=7))
    ingests = await db.recent_ingests(state)
    invites = await auth.pending_invites()
    llm_status: dict[str, Any]
    try:
        llm_status = await llm.health()
    except Exception as e:
        llm_status = {"error": str(e)}
    return templates.TemplateResponse(
        request,
        "admin.html",
        {
            "login": login,
            "states": STATES,
            "state": state,
            "counts": counts,
            "ingests": ingests,
            "invites": invites,
            "invite_days": auth.INVITE_TTL.days,
            "llm": llm_status,
            "restate_ui_url": settings.restate_ui_url,
            **extra,
        },
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, login: Admin, state: str | None = None) -> Response:
    return await render(request, login, state)


# Posted to the page's own URL: Serve strips the /admin prefix, so the app can't build links to it.
@app.post("/", response_class=HTMLResponse, dependencies=[SameOrigin])
async def create_invite(
    request: Request,
    login: Admin,
    email: Annotated[str, Form()],
    days: Annotated[float, Form(gt=0, le=90)] = auth.INVITE_TTL.days,
) -> Response:
    email = db.normalise_email(email)
    if "@" not in email:
        raise HTTPException(400, "that doesn't look like an email address")
    token = await auth.create_invite(email, timedelta(days=days))
    # Shown once, in this response only: the database keeps just the token's hash.
    link = f"{settings.public_base_url}/register/{token}"
    return await render(request, login, invite_link=link, invite_for=email)
