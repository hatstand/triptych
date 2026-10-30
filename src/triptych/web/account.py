"""/account: a signed-in user's passkeys, the addresses they send plans from, and eval set sharing."""

from typing import Annotated

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from triptych import auth, db, senders
from triptych.config import settings
from triptych.services import eval_share as eval_share_svc
from triptych.web.deps import Client, User, templates

router = APIRouter(prefix="/account")


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


@router.get("", response_class=HTMLResponse)
async def account_page(request: Request, me: User) -> Response:
    return await render_account(request, me)


@router.get("/{tab}", response_class=HTMLResponse)
async def account_tab(request: Request, tab: str, me: User) -> Response:
    if tab not in account_tabs():
        raise HTTPException(404)
    if tab == next(iter(ACCOUNT_TABS)):
        return RedirectResponse("/account", status_code=308)
    return await render_account(request, me, tab)


@router.post("/passkeys/{passkey_id}/delete", response_class=HTMLResponse)
async def passkey_delete(request: Request, passkey_id: str, me: User) -> Response:
    try:
        await auth.delete_passkey(me.id, passkey_id)
    except auth.AuthError as e:
        return await render_account(request, me, error=str(e))
    return RedirectResponse("/account", status_code=303)


@router.post("/senders", response_class=HTMLResponse)
async def sender_add(request: Request, me: User, email: Annotated[str, Form()]) -> Response:
    try:
        code = await senders.request(me.id, email)
    except senders.SenderError as e:
        return await render_account(request, me, "senders", sender_error=str(e))
    return await render_account(request, me, "senders", new_sender=(db.normalise_email(email), code))


@router.post("/senders/delete", response_class=HTMLResponse)
async def sender_delete(request: Request, me: User, email: Annotated[str, Form()]) -> Response:
    try:
        await senders.remove(me.id, email)
    except senders.SenderError as e:
        return await render_account(request, me, "senders", sender_error=str(e))
    return RedirectResponse("/account/senders", status_code=303)


@router.post("/sharing")
async def sharing_set(me: User, client: Client, share: Annotated[bool, Form()]) -> Response:
    """Opting in copies everything already kept, then every new ingest (services/eval_share.py)."""
    if "sharing" not in account_tabs():
        raise HTTPException(404)
    await db.set_eval_sharing(me.id, share)
    if share:
        await client.object_send(eval_share_svc.share_all, me.id, None)
    return RedirectResponse("/account/sharing", status_code=303)
