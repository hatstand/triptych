"""`Reprocess` workflow (key = "<batch id>:<ingest id>"): an ingest done again, to see what a new
model, prompt or normalise would make of it, and optionally to make it so.

    re-extract: load the kept original ─▶ Llm.extract with `model` ─▶ record run ─┐
    re-derive:  a stored run's output (no GPU) ─────────────────────────────────┴▶ normalise
        ─▶ dry_run / shadow: work out the diff        ─▶ save it (db reprocess)
        ─▶ apply: User.reconcile (diff, then make it)  ─┘

The diff matches the new reservations to the ones the ingest already made (db.reconcile). Applying
updates matches in place, files new ones, and never deletes: what the user deleted stays deleted,
and what the new run leaves out is only reported. Admin starts these in batches (/admin), and
Ingest starts a shadow one per new ingest when SHADOW_MODEL is set.

Re-extraction runs in the same `gpu` scope as Ingest, under its own "reprocess" limit key per user,
so a big batch takes turns with live mail instead of queueing in front of it.

The owner starts one too, by saying a "no travel" ingest is a booking (the inbox's "This is a
booking", batch OWNER_BATCH): it is extracted again with the model told so, as interactive work,
and then copied to the eval set and made a worked example like any other correction.
"""

from datetime import timedelta

import restate

from triptych import db, enrich, inputs
from triptych.models import (
    Extraction,
    LlmExtractRequest,
    ParsedDocument,
    ReconcileRequest,
    ReprocessRequest,
    Reservation,
)
from triptych.services import eval_share
from triptych.services.llm_service import GPU_SCOPE, limit_key, record_run
from triptych.services.llm_service import extract as llm_extract
from triptych.services.user import reconcile

reprocess = restate.Workflow("Reprocess")

OWNER_BATCH = "owner-says-travel"
"""The batch of the ones owners start."""


async def _extract(
    ctx: restate.WorkflowContext, req: ReprocessRequest, target: db.ReprocessTarget, model: str
) -> tuple[str, Extraction]:
    if target.input_id is None:
        raise restate.TerminalError("no original was kept for this ingest")
    doc = await ctx.run_typed(
        "load original", inputs.load_original, restate.RunOptions[ParsedDocument](max_attempts=5),
        target.input_id,
    )  # fmt: skip
    run_id = str(ctx.uuid())
    purpose = "shadow" if req.mode == "shadow" else "reprocess"
    hint = req.owner_says_travel
    try:
        extraction = await ctx.scope(GPU_SCOPE).service_call(
            llm_extract,
            LlmExtractRequest(
                document=doc, received_at=target.received_at, model=model, owner_says_travel=hint
            ),
            limit_key=limit_key("interactive" if hint else "reprocess", target.owner),
        )
    except restate.TerminalError as e:
        await ctx.run_typed(
            "record run", record_run, restate.RunOptions[None](), run_id, req.ingest_id, target.owner,
            target.input_id, model, purpose, None, str(e)[:2000], None, hint,
        )  # fmt: skip
        raise
    await ctx.run_typed(
        "record run", record_run, restate.RunOptions[None](), run_id, req.ingest_id, target.owner,
        target.input_id, model, purpose, extraction, None, None, hint,
    )  # fmt: skip
    return run_id, extraction


@reprocess.main(inactivity_timeout=timedelta(minutes=5))
async def run(ctx: restate.WorkflowContext, req: ReprocessRequest) -> None:
    run_id: str | None = None
    try:
        target = await ctx.run_typed(
            "ingest", db.reprocess_target, restate.RunOptions[db.ReprocessTarget](), req.ingest_id
        )
        await ctx.run_typed("queued", db.add_reprocess, restate.RunOptions[None](), req, target.owner)
        if req.from_run_id:
            run_id = req.from_run_id
            extraction = await ctx.run_typed(
                "stored output", db.run_output, restate.RunOptions[Extraction](), run_id
            )
        elif req.model:
            run_id, extraction = await _extract(ctx, req, target, req.model)
        else:
            raise restate.TerminalError("give a model to extract with, or a run to re-derive from")

        candidates: list[Reservation] = []
        for x in extraction.reservations:
            r = enrich.normalise(x, reservation_id=str(ctx.uuid()), ingest_id=req.ingest_id, run_id=run_id)
            if r is not None:
                candidates.append(r)
        if req.mode == "apply":
            rec = await ctx.object_call(
                reconcile,
                target.owner,
                ReconcileRequest(ingest_id=req.ingest_id, run_id=run_id, reservations=candidates),
            )
            if req.owner_says_travel:  # reconcile has remade its example; now its eval label
                ctx.object_send(eval_share.copy, target.owner, req.ingest_id)
        else:
            rec = await ctx.run_typed(
                "diff", db.preview_reprocess, restate.RunOptions[db.Reconciliation](), target.owner,
                req.ingest_id, candidates,
            )  # fmt: skip
        await ctx.run_typed(
            "save diff", db.finish_reprocess, restate.RunOptions[None](), req.key, run_id, rec, None
        )
    except restate.TerminalError as e:
        await ctx.run_typed(
            "save error", db.finish_reprocess, restate.RunOptions[None](), req.key, run_id, None, str(e)[:500]
        )
