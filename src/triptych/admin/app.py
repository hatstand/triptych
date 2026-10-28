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

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Literal, TypedDict, cast, get_args

import restate
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field, ValidationError

from triptych import auth, db, llm, signup, telemetry
from triptych.config import settings
from triptych.models import IngestState, ReprocessRequest
from triptych.services import reprocess
from triptych.web import csp
from triptych.web.assets import Assets

HERE = Path(__file__).parent
LOGIN_HEADER = "Tailscale-User-Login"
STATES: tuple[str, ...] = get_args(IngestState)
CORRECTION_DAYS = 30


class AppState(TypedDict):
    """What the lifespan hands every request (Starlette's lifespan state)."""

    restate: restate.RestateClient


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[AppState]:
    telemetry.setup("triptych-admin")
    db.init()
    async with restate.create_client(settings.restate_ingress_url) as client:
        yield {"restate": client}
    await db.close()


app = FastAPI(title="Triptych admin", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
app.middleware("http")(csp.content_security_policy)
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


def rs(request: Request) -> restate.RestateClient:
    return cast(AppState, request.state)["restate"]


async def render(
    request: Request, login: str, state: str | None = None, batch: str | None = None, **extra: Any
) -> Response:
    if state is not None and state not in STATES:
        raise HTTPException(400, f"state must be one of {', '.join(STATES)}")
    counts = await db.ingest_counts(datetime.now(UTC) - timedelta(days=7))
    ingests = await db.recent_ingests(state)
    invites = await auth.pending_invites()
    waiting = await signup.waitlist()
    batches = await db.reprocess_batches()
    items = await db.reprocess_batch(batch) if batch else []
    rates = await db.correction_rates(datetime.now(UTC) - timedelta(days=CORRECTION_DAYS))
    worked = await db.admin_examples()
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
            "waiting": waiting,
            "signups": settings.signups,
            "llm": llm_status,
            "llm_model": settings.llm_model,
            "batches": batches,
            "batch": batch,
            "items": items,
            "rates": rates,
            "correction_days": CORRECTION_DAYS,
            "llm_examples": settings.llm_examples,
            "examples": worked,
            "restate_ui_url": settings.restate_ui_url,
            **extra,
        },
    )


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request, login: Admin, state: str | None = None, batch: str | None = None
) -> Response:
    return await render(request, login, state, batch)


class ApproveForm(BaseModel):
    user: str
    email: str = ""


class InviteForm(BaseModel):
    email: str
    days: float = Field(auth.INVITE_TTL.days, gt=0, le=90)


class ReprocessForm(BaseModel):
    """Which ingests to do again, and how (services/reprocess.py)."""

    source: Literal["extract", "stored"]
    """Extract the original again with `model`, or re-derive from each ingest's current run."""
    model: str = ""
    mode: Literal["dry_run", "apply"] = "dry_run"
    ingest: str = ""
    """Just this one ingest (its row's buttons in the ingest list), instead of the filter below."""
    state: Literal["", "done", "no_travel", "failed"] = ""
    email: str = ""
    since: date | None = None
    limit: int = Field(50, ge=1, le=500)


class ApplyForm(BaseModel):
    batch: str


class RemoveExampleForm(BaseModel):
    ingest: str


def new_batch() -> str:
    return uuid.uuid4().hex[:12]


async def start(client: restate.RestateClient, req: ReprocessRequest, owner: str) -> None:
    await db.add_reprocess(req, owner)  # so the batch shows its queued items straight away
    await client.workflow_send(reprocess.run, req.key, req)


async def start_batch(client: restate.RestateClient, f: ReprocessForm) -> tuple[str, int]:
    batch = new_batch()
    found = await db.reprocess_candidates(
        ingest_id=f.ingest.strip() or None,
        state=f.state or None,
        email=f.email.strip() or None,
        since=f.since,
        limit=f.limit,
    )
    n = 0
    for c in found:
        if f.source == "stored":
            if c.current_run_id is None:
                continue
            req = ReprocessRequest(
                batch_id=batch, ingest_id=c.ingest_id, mode=f.mode, from_run_id=c.current_run_id
            )
        else:
            if c.input_id is None:
                continue  # from before originals were kept
            model = f.model.strip() or settings.llm_model
            req = ReprocessRequest(batch_id=batch, ingest_id=c.ingest_id, mode=f.mode, model=model)
        await start(client, req, c.owner)
        n += 1
    return batch, n


async def apply_batch(client: restate.RestateClient, f: ApplyForm) -> tuple[str, int]:
    """Apply what a dry run found: re-derive from the runs it recorded, so nothing is extracted again."""
    batch = new_batch()
    runs = await db.reprocess_runs(f.batch)
    for ingest_id, owner, run_id in runs:
        await start(
            client,
            ReprocessRequest(batch_id=batch, ingest_id=ingest_id, mode="apply", from_run_id=run_id),
            owner,
        )
    return batch, len(runs)


# Every form posts to the page's own URL (Serve strips the /admin prefix, so the app can't build
# links to it), and says which it is in `action`.
@app.post("/", response_class=HTMLResponse, dependencies=[SameOrigin])
async def post(request: Request, login: Admin) -> Response:
    # A field left blank is a field not given.
    form = {k: v for k, v in (await request.form()).items() if isinstance(v, str) and v.strip()}
    action = form.pop("action", "invite")
    try:
        match action:
            case "reprocess":
                batch, n = await start_batch(rs(request), ReprocessForm.model_validate(form))
                return await render(request, login, batch=batch, started=n)
            case "apply":
                batch, n = await apply_batch(rs(request), ApplyForm.model_validate(form))
                return await render(request, login, batch=batch, started=n)
            case "remove_example":
                removed = RemoveExampleForm.model_validate(form).ingest
                await db.delete_example(removed)
                return await render(request, login, removed_example=removed)
            case "invite":
                return await create_invite(request, login, InviteForm.model_validate(form))
            case "approve":
                approved = ApproveForm.model_validate(form)
                await signup.approve(approved.user)
                return await render(request, login, approved=approved.email)
            case _:
                raise HTTPException(400, f"unknown action {action}")
    except ValidationError as e:
        raise HTTPException(422, str(e)) from e


async def create_invite(request: Request, login: str, f: InviteForm) -> Response:
    email = db.normalise_email(f.email)
    if "@" not in email:
        raise HTTPException(400, "that doesn't look like an email address")
    token = await auth.create_invite(email, timedelta(days=f.days))
    # Shown once, in this response only: the database keeps just the token's hash.
    link = f"{settings.public_base_url}/register/{token}"
    return await render(request, login, invite_link=link, invite_for=email)
