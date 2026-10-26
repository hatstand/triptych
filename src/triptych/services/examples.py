"""`Examples` virtual object (key = app_user id): keeps a user's worked examples for extraction in
step with their corrections (examples.py). One at a time per user, so two quick edits to the same
ingest are applied in order and the later one wins.

    refresh  (User, on an edit, a delete or a reprocess)  rebuild that ingest's example

Best effort, like the eval set's copies: one that keeps failing is logged and skipped, and the
next correction to that ingest tries again.
"""

import logging
from datetime import timedelta

import restate

from triptych import examples as worked

log = logging.getLogger("triptych.examples")

examples = restate.VirtualObject("Examples")

REFRESH_RETRY = restate.RunOptions[bool](
    max_duration=timedelta(minutes=30),
    initial_retry_interval=timedelta(seconds=2),
    max_retry_interval=timedelta(minutes=2),
    retry_interval_factor=2.0,
)


@examples.handler()
async def refresh(ctx: restate.ObjectContext, ingest_id: str) -> bool:
    try:
        return await ctx.run_typed(f"refresh {ingest_id}", worked.refresh, REFRESH_RETRY, ingest_id)
    except restate.TerminalError as e:
        log.error("examples: %s not refreshed: %s", ingest_id, e)
        return False
