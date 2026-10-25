"""`EvalShare` virtual object (key = app_user id): copies a user's originals into the extraction
eval set while they share them (evalset.py). One at a time per user, so opting in, which copies
everything they have, and the copy of each new ingest never race over the same case.

    share_all  (on opting in)     every ingest with a kept original that isn't copied yet
    copy       (Ingest, at end)   one ingest, if its owner shares

Copies are best effort: one that keeps failing (R2 down for half an hour) is skipped and logged,
and the next share_all picks it up again.
"""

import logging
from datetime import timedelta

import restate

from triptych import evalset

log = logging.getLogger("triptych.eval_share")

eval_share = restate.VirtualObject("EvalShare")

COPY_RETRY = restate.RunOptions[bool](
    max_duration=timedelta(minutes=30),
    initial_retry_interval=timedelta(seconds=2),
    max_retry_interval=timedelta(minutes=2),
    retry_interval_factor=2.0,
)


async def _copy(ctx: restate.ObjectContext, ingest_id: str) -> bool:
    try:
        return await ctx.run_typed(f"copy {ingest_id}", evalset.copy, COPY_RETRY, ingest_id)
    except restate.TerminalError as e:
        log.error("eval set: %s not copied: %s", ingest_id, e)
        return False


@eval_share.handler()
async def share_all(ctx: restate.ObjectContext) -> int:
    """Copy everything this user hasn't had copied yet. Returns how many were copied."""
    pending = await ctx.run_typed(
        "list uncopied", evalset.uncopied, restate.RunOptions[list[str]](), ctx.key()
    )
    copied = 0
    for ingest_id in pending:
        # Each copy checks they still share, so opting out part way through stops the rest.
        copied += await _copy(ctx, ingest_id)
    return copied


@eval_share.handler()
async def copy(ctx: restate.ObjectContext, ingest_id: str) -> bool:
    return await _copy(ctx, ingest_id)
