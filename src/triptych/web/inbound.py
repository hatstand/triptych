"""/inbound: mail from the Cloudflare Email Worker, to file as plans or to prove an address."""

import asyncio
import base64
import hmac
import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, HTTPException, Request

from triptych import db, inbound, senders, signup
from triptych.config import settings
from triptych.models import IngestRequest, Notification
from triptych.services import ingest as ingest_svc
from triptych.web.deps import Client

MAX_INBOUND = 25 * 1024 * 1024  # Cloudflare Email Routing's own limit

router = APIRouter()


@router.post("/inbound")
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
