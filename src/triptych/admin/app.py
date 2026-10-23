"""Admin pages, authorised by Tailscale identity instead of the app's own sign-in.

This is a separate ASGI app that listens only on a Unix socket (compose service `admin`), and
`tailscale serve` on marvin proxies https://triptych.<tailnet>.ts.net/admin to that socket. Serve
deletes any client-supplied Tailscale-User-* headers and sets Tailscale-User-Login itself, and only
for tailnet peers owned by a user (tagged devices and Funnel traffic get none). Nothing else can
reach the socket: it is not a TCP port, so no Funnel route, compose neighbour or future public
proxy pointed at the web app can get a forged header in here. The web app never reads the header.

Read-only on purpose, so a cross-site form post from another tab on a tailnet device has nothing to
trigger. Anything that writes (invites, passkeys) needs a same-origin check first.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, get_args

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from triptych import db, llm
from triptych.config import settings
from triptych.models import IngestState

HERE = Path(__file__).parent
LOGIN_HEADER = "Tailscale-User-Login"
STATES: tuple[str, ...] = get_args(IngestState)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
    db.init()
    yield
    await db.close()


app = FastAPI(title="Triptych admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
# Admin templates extend the web app's base.html; /static is served by the web app on the same host.
templates = Jinja2Templates(directory=[HERE / "templates", HERE.parent / "web" / "templates"])


def tailscale_admin(request: Request) -> str:
    login = request.headers.get(LOGIN_HEADER, "").strip().lower()
    if not login:
        raise HTTPException(403, "no Tailscale identity: open this from a device signed in to the tailnet")
    if login not in settings.admin_logins:
        raise HTTPException(403, f"{login} is not in ADMIN_TAILSCALE_LOGINS")
    return login


Admin = Annotated[str, Depends(tailscale_admin)]


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, login: Admin, state: str | None = None) -> Response:
    if state is not None and state not in STATES:
        raise HTTPException(400, f"state must be one of {', '.join(STATES)}")
    counts = await db.ingest_counts(datetime.now(UTC) - timedelta(days=7))
    ingests = await db.recent_ingests(state)
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
            "llm": llm_status,
        },
    )
