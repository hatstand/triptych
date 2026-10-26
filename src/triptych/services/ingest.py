"""`Ingest` workflow (key = ingest id): one forwarded email / upload → reservations filed into trips.

    keep original ─▶ parse ─▶ (LLM_EXAMPLES on) the owner's examples
        ─▶ Llm.extract (scope "gpu": flow-controlled + retried) ─▶ record run
        ─▶ normalise ─▶ User.file_reservations ─▶ (SHADOW_MODEL set) start a shadow Reprocess
        ─▶ (original kept) EvalShare.copy, a no-op unless the owner shares with the eval set

The original is kept first (inputs.py), so it survives even when extraction fails, and every
extraction is recorded with the model and prompt that made it (db extraction_run).

Each step is journaled, so a crash mid-way resumes after the last completed step and the (slow,
expensive) LLM call is never repeated once it has succeeded. Keying by a stable id (the email's
Message-ID for inbound mail) makes re-delivery of the same email a no-op.
"""

import logging
from datetime import timedelta

import restate

from triptych import db, enrich, examples, inputs, parsing
from triptych.config import settings
from triptych.models import (
    FileRequest,
    InboxItem,
    IngestRequest,
    LlmExtractRequest,
    ParsedDocument,
    ReprocessRequest,
)
from triptych.services import eval_share, reprocess
from triptych.services.llm_service import GPU_SCOPE, limit_key, record_run
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

EXAMPLES_RETRY = restate.RunOptions[LlmExtractRequest](max_attempts=3)


def _label(req: IngestRequest) -> str:
    return req.subject or req.filename or f"{req.source} {req.received_at:%Y-%m-%d %H:%M}"


async def shadow_model() -> str | None:
    return settings.shadow_model


def queued_item(ingest_id: str, req: IngestRequest) -> InboxItem:
    """How an ingest first appears in the inbox."""
    return InboxItem(
        ingest_id=ingest_id, source=req.source, label=_label(req), received_at=req.received_at, state="queued"
    )


async def shadow(ctx: restate.WorkflowContext, input_id: str | None) -> None:
    """With SHADOW_MODEL set, extract every kept original again with that model too, and record
    what it would have changed without changing anything (Reprocess, mode "shadow")."""
    model = await ctx.run_typed("shadow model", shadow_model, restate.RunOptions[str | None]())
    if model and input_id:
        req = ReprocessRequest(batch_id="shadow", ingest_id=ctx.key(), mode="shadow", model=model)
        ctx.workflow_send(reprocess.run, req.key, req)


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

    input_id: str | None = None
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
        extract_req = LlmExtractRequest(document=doc, received_at=req.received_at)
        if settings.llm_examples:  # with it off, the journal is just as it was before examples
            try:
                extract_req = await ctx.run_typed(
                    "examples", examples.with_examples, EXAMPLES_RETRY, extract_req, ctx.key(), req
                )
            except restate.TerminalError as e:
                # Examples only help, so extract without them rather than not at all.
                log.error("ingest %s: no examples: %s", ctx.key(), e)
        shown = [ex.ingest_id for ex in extract_req.examples]
        try:
            extraction = await ctx.scope(GPU_SCOPE).service_call(
                llm_extract, extract_req, limit_key=limit_key("batch", req.user)
            )
        except restate.TerminalError as e:
            await ctx.run_typed(
                "record run", record_run, restate.RunOptions[None](), run_id, ctx.key(), req.user,
                input_id, settings.llm_model, "ingest", None, str(e)[:2000], shown,
            )  # fmt: skip
            raise
        await ctx.run_typed(
            "record run", record_run, restate.RunOptions[None](), run_id, ctx.key(), req.user, input_id,
            settings.llm_model, "ingest", extraction, None, shown,
        )  # fmt: skip

        reservations = [
            r
            for x in extraction.reservations
            if (r := enrich.normalise(x, reservation_id=str(ctx.uuid()), ingest_id=ctx.key(), run_id=run_id))
            is not None
        ]
        if not reservations:
            await publish(state="no_travel", message="No travel reservations found in this document.")
        else:
            await publish(state="filing")
            filed = await ctx.object_call(
                file_reservations, req.user, FileRequest(ingest_id=ctx.key(), reservations=reservations)
            )
            n = len(reservations)
            await publish(
                state="done", trip_ids=filed.trip_ids, message=f"Added {n} reservation{'s' * (n != 1)}."
            )
        await shadow(ctx, input_id)
    except restate.TerminalError as e:
        await publish(state="failed", message=str(e)[:500])
    if input_id:
        # Failed and no-travel ingests too: they're the cases the eval set most needs.
        ctx.object_send(eval_share.copy, req.user, ctx.key())
    return item


@ingest.handler()
async def status(ctx: restate.WorkflowSharedContext) -> InboxItem | None:
    return await ctx.get("status", type_hint=InboxItem)
