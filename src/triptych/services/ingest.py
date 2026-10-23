"""`Ingest` workflow (key = ingest id): one forwarded email / upload → reservations filed into trips.

    parse ─▶ Llm.extract (scope "gpu": flow-controlled + retried) ─▶ normalise ─▶ User.file_reservations

Each step is journaled, so a crash mid-way resumes after the last completed step and the (slow,
expensive) LLM call is never repeated once it has succeeded. Keying by a stable id (the email's
Message-ID for inbound mail) makes re-delivery of the same email a no-op.
"""

from datetime import timedelta

import restate

from triptych import db, enrich, parsing
from triptych.models import FileRequest, InboxItem, IngestRequest, LlmExtractRequest, ParsedDocument
from triptych.services.llm_service import GPU_SCOPE, limit_key
from triptych.services.llm_service import extract as llm_extract
from triptych.services.user import file_reservations

ingest = restate.Workflow("Ingest")


def _label(req: IngestRequest) -> str:
    return req.subject or req.filename or f"{req.source} {req.received_at:%Y-%m-%d %H:%M}"


def queued_item(ingest_id: str, req: IngestRequest) -> InboxItem:
    """How an ingest first appears in the inbox."""
    return InboxItem(
        ingest_id=ingest_id, source=req.source, label=_label(req), received_at=req.received_at, state="queued"
    )


@ingest.main(inactivity_timeout=timedelta(minutes=5))
async def run(ctx: restate.WorkflowContext, req: IngestRequest) -> InboxItem:
    item = queued_item(ctx.key(), req)

    async def publish(**changes: object) -> None:
        nonlocal item
        item = item.model_copy(update=changes)
        ctx.set("status", item)
        await ctx.run_typed(
            f"inbox: {item.state}", db.upsert_inbox, restate.RunOptions[None](), req.user, item
        )

    try:
        await publish(state="parsing")
        doc = await ctx.run_typed(
            "parse", parsing.parse, restate.RunOptions[ParsedDocument](max_attempts=2), req
        )
        if doc.subject and not req.subject:
            await publish(label=doc.subject)

        await publish(state="extracting")
        extraction = await ctx.scope(GPU_SCOPE).service_call(
            llm_extract,
            LlmExtractRequest(document=doc, received_at=req.received_at),
            limit_key=limit_key("batch", req.user),
        )

        reservations = [
            r
            for x in extraction.reservations
            if (r := enrich.normalise(x, reservation_id=str(ctx.uuid()), ingest_id=ctx.key())) is not None
        ]
        if not reservations:
            await publish(state="no_travel", message="No travel reservations found in this document.")
            return item

        await publish(state="filing")
        filed = await ctx.object_call(
            file_reservations, req.user, FileRequest(ingest_id=ctx.key(), reservations=reservations)
        )
        n = len(reservations)
        await publish(
            state="done", trip_ids=filed.trip_ids, message=f"Added {n} reservation{'s' * (n != 1)}."
        )
    except restate.TerminalError as e:
        await publish(state="failed", message=str(e)[:500])
    return item


@ingest.handler()
async def status(ctx: restate.WorkflowSharedContext) -> InboxItem | None:
    return await ctx.get("status", type_hint=InboxItem)
