"""Inbound SMTP gateway — the `plans@tripit.com` equivalent.

Forward a confirmation email to plans@<anything> on port 2525 and it is filed under the *sender's*
address, exactly like TripIt. Each message starts an `Ingest` workflow keyed by a hash of its
(sender, Message-ID), so retried deliveries are deduplicated by Restate.
"""

import asyncio
import base64
import email
import email.policy
import logging
import signal
from datetime import UTC, datetime
from email.utils import parseaddr

import restate
from aiosmtpd.controller import Controller
from aiosmtpd.smtp import SMTP, Envelope, Session

from triptych import db, telemetry
from triptych.config import settings
from triptych.inbound import ingest_id_for
from triptych.models import IngestRequest
from triptych.services.ingest import run as ingest_run

log = logging.getLogger("triptych.smtp")


class Handler:
    async def handle_DATA(self, server: SMTP, session: Session, envelope: Envelope) -> str:
        raw = envelope.original_content or envelope.content
        if not isinstance(raw, bytes):
            return "554 empty message"
        _, sender = parseaddr(envelope.mail_from or "")
        if not sender:
            return "550 sender address required"
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        req = IngestRequest(
            user=await db.user_for_email(sender),
            source="email",
            subject=str(msg.get("subject") or "") or None,
            content_type="message/rfc822",
            data_b64=base64.b64encode(raw).decode(),
            received_at=datetime.now(UTC),
        )
        key = ingest_id_for(raw, sender.lower())
        try:
            async with restate.create_client(settings.restate_ingress_url) as client:
                await client.workflow_send(ingest_run, key, req)
        except Exception:
            log.exception("failed to hand message to restate")
            return "451 temporary failure, try again later"
        log.info("queued %s from %s (%d bytes)", key, sender, len(raw))
        return "250 Message accepted for delivery"


async def main() -> None:
    telemetry.setup("triptych-smtp")
    controller = Controller(Handler(), hostname="0.0.0.0", port=2525, data_size_limit=25 * 1024 * 1024)
    controller.start()
    log.info("SMTP gateway listening on :2525")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    await stop.wait()
    controller.stop()


if __name__ == "__main__":
    asyncio.run(main())
