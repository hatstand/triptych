"""`Ingest` workflow (key = ingest id): one forwarded email / upload → reservations filed into trips.

    keep original ─▶ parse ─▶ Llm.extract (scope "gpu": flow-controlled + retried) ─▶ record run
        ─▶ normalise ─▶ User.file_reservations

The original is kept first (inputs.py), so it survives even when extraction fails, and every
extraction is recorded with the model and prompt that made it (db extraction_run).

Each step is journaled, so a crash mid-way resumes after the last completed step and the (slow,
expensive) LLM call is never repeated once it has succeeded. Keying by a stable id (the email's
Message-ID for inbound mail) makes re-delivery of the same email a no-op.
"""

import logging
from datetime import timedelta

import restate

from triptych import db, enrich, inputs, llm, parsing
from triptych.config import settings
from triptych.models import (
    Extraction,
    ExtractionRun,
    FileRequest,
    InboxItem,
    IngestRequest,
    LlmExtractRequest,
    ParsedDocument,
)
from triptych.services.llm_service import GPU_SCOPE, limit_key
from triptych.services.llm_service import extract as llm_extract
from triptych.services.user import file_reservations

log = logging.getLogger("triptych.ingest")

ingest = restate.Workflow("Ingest")

# Long enough to ride out an R2 outage; after that the ingest goes on without its original.
KEEP_RETRY = restate.RunOptions[str | None](
    max_duration=timedelta(minutes=30),
    initial_retry_interval=timedelta(seconds=2),
    max_retry_interval=timedelta(minutes=2),
    retry_interval_factor=2.0,
)


def _label(req: IngestRequest) -> str:
    return req.subject or req.filename or f"{req.source} {req.received_at:%Y-%m-%d %H:%M}"


async def record_run(
    run_id: str,
    ingest_id: str,
    req: IngestRequest,
    input_id: str | None,
    output: Extraction | None,
    error: str | None,
) -> None:
    try:
        digest = await llm.model_digest()
    except Exception:
        log.warning("couldn't get the digest of %s", settings.llm_model, exc_info=True)
        digest = None
    await db.add_extraction_run(
        ExtractionRun(
            id=run_id,
            ingest_id=ingest_id,
            owner=req.user,
            input_id=input_id,
            model=settings.llm_model,
            model_digest=digest,
            prompt_version=llm.prompt_version(),
            code_version=settings.code_version,
            params=llm.chat_params(),
            purpose="ingest",
            output=output,
            error=error,
        )
    )


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
        try:
            input_id = await ctx.run_typed("keep original", inputs.keep, KEEP_RETRY, ctx.key(), req)
        except restate.TerminalError as e:
            log.error("ingest %s: original not kept: %s", ctx.key(), e)
            input_id = None

        doc = await ctx.run_typed(
            "parse", parsing.parse, restate.RunOptions[ParsedDocument](max_attempts=2), req
        )
        if doc.subject and not req.subject:
            await publish(label=doc.subject)

        await publish(state="extracting")
        run_id = str(ctx.uuid())
        try:
            extraction = await ctx.scope(GPU_SCOPE).service_call(
                llm_extract,
                LlmExtractRequest(document=doc, received_at=req.received_at),
                limit_key=limit_key("batch", req.user),
            )
        except restate.TerminalError as e:
            await ctx.run_typed(
                "record run", record_run, restate.RunOptions[None](), run_id, ctx.key(), req, input_id,
                None, str(e)[:2000],
            )  # fmt: skip
            raise
        await ctx.run_typed(
            "record run", record_run, restate.RunOptions[None](), run_id, ctx.key(), req, input_id,
            extraction, None,
        )  # fmt: skip

        reservations = [
            r
            for x in extraction.reservations
            if (r := enrich.normalise(x, reservation_id=str(ctx.uuid()), ingest_id=ctx.key(), run_id=run_id))
            is not None
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
