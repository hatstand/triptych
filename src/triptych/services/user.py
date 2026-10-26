"""`User` virtual object (key = app_user id): the single writer for a user's trips.

The data lives in Postgres; this object exists for ordering. Exclusive handlers run one at a time
per user, so two confirmation emails arriving together can't both decide to create "the Lisbon
trip", and a delete from the web UI can't interleave with a filing that touches the same trip.
Each write is one `ctx.run` step wrapping an idempotent DB transaction (see db.py).
"""

import restate

from triptych import db
from triptych.models import (
    EditReservation,
    FileRequest,
    FileResult,
    MergeTrips,
    ReconcileRequest,
    RemoveReservation,
    RenameTrip,
    TripRef,
)
from triptych.services import eval_share, examples, place_photo, reminders

user = restate.VirtualObject("User")


def corrected(ctx: restate.ObjectContext, ingest_id: str) -> None:
    """The owner corrected what an ingest filed: a label for the eval set, if they share with it,
    and a worked example for their own later mail."""
    ctx.object_send(eval_share.copy, ctx.key(), ingest_id)
    ctx.object_send(examples.refresh, ctx.key(), ingest_id)


@user.handler()
async def file_reservations(ctx: restate.ObjectContext, req: FileRequest) -> FileResult:
    filed = await ctx.run_typed(
        "file in postgres",
        db.file_reservations,
        restate.RunOptions[db.Filed](),
        ctx.key(),
        req.ingest_id,
        req.reservations,
    )
    await reminders.schedule(ctx, ctx.key(), filed.reservations)
    await place_photo.request(ctx, filed.trip_ids)
    return FileResult(trip_ids=filed.trip_ids)


@user.handler()
async def reconcile(ctx: restate.ObjectContext, req: ReconcileRequest) -> db.Reconciliation:
    """Make a reprocessed run the ingest's current one (services/reprocess.py)."""
    done = await ctx.run_typed(
        "reconcile in postgres",
        db.apply_reprocess,
        restate.RunOptions[db.Reprocessed](),
        ctx.key(),
        req.ingest_id,
        req.run_id,
        req.reservations,
    )
    await reminders.schedule(ctx, ctx.key(), done.filed.reservations)
    await place_photo.request(ctx, done.filed.trip_ids)
    # The owner's corrections now stand against the new run's answer.
    ctx.object_send(examples.refresh, ctx.key(), req.ingest_id)
    return done.reconciliation


@user.handler()
async def remove_reservation(ctx: restate.ObjectContext, req: RemoveReservation) -> bool:
    """Returns whether the trip still exists."""
    removed = await ctx.run_typed(
        "remove reservation",
        db.remove_reservation,
        restate.RunOptions[db.Removed](),
        ctx.key(),
        req.trip_id,
        req.reservation_id,
    )
    if removed.trip_exists:
        # Removing the Porto hotel from a Lisbon & Porto trip can move its destination.
        await place_photo.request(ctx, [req.trip_id])
    if removed.ingest_id:
        # A deleted reservation is one its ingest shouldn't have produced: a correction to learn from.
        corrected(ctx, removed.ingest_id)
    return removed.trip_exists


@user.handler()
async def edit_reservation(ctx: restate.ObjectContext, req: EditReservation) -> None:
    filed = await ctx.run_typed(
        "edit reservation",
        db.edit_reservation,
        restate.RunOptions[db.Filed | None](),
        ctx.key(),
        req.trip_id,
        req.reservation_id,
        req.fields,
    )
    if filed is not None:
        # A new time is a new reminder; the old one finds the time changed and does nothing.
        await reminders.schedule(ctx, ctx.key(), filed.reservations)
        # A new city can move the trip's destination.
        await place_photo.request(ctx, filed.trip_ids)
        edited = next(
            (f.reservation for f in filed.reservations if f.reservation.id == req.reservation_id), None
        )
        if edited and edited.source_ingest_id:
            corrected(ctx, edited.source_ingest_id)


@user.handler()
async def rename_trip(ctx: restate.ObjectContext, req: RenameTrip) -> None:
    await ctx.run_typed(
        "rename trip", db.rename_trip, restate.RunOptions[None](), ctx.key(), req.trip_id, req.name
    )


@user.handler()
async def merge_trips(ctx: restate.ObjectContext, req: MergeTrips) -> None:
    await ctx.run_typed(
        "merge trips", db.merge_trips, restate.RunOptions[None](), ctx.key(), req.trip_id, req.into
    )
    # The merged trip can have a new destination, so a new photo. Reminders are keyed by reservation
    # id and time, neither of which a merge changes.
    await place_photo.request(ctx, [req.into])


@user.handler()
async def delete_trip(ctx: restate.ObjectContext, req: TripRef) -> None:
    await ctx.run_typed("delete trip", db.delete_trip, restate.RunOptions[None](), ctx.key(), req.trip_id)
