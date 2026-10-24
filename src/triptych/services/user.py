"""`User` virtual object (key = app_user id): the single writer for a user's trips.

The data lives in Postgres; this object exists for ordering. Exclusive handlers run one at a time
per user, so two confirmation emails arriving together can't both decide to create "the Lisbon
trip", and a delete from the web UI can't interleave with a filing that touches the same trip.
Each write is one `ctx.run` step wrapping an idempotent DB transaction (see db.py).
"""

import restate

from triptych import db
from triptych.models import FileRequest, FileResult, ReconcileRequest, RemoveReservation, RenameTrip, TripRef
from triptych.services import place_photo, reminders

user = restate.VirtualObject("User")


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
    return done.reconciliation


@user.handler()
async def remove_reservation(ctx: restate.ObjectContext, req: RemoveReservation) -> bool:
    """Returns whether the trip still exists."""
    exists = await ctx.run_typed(
        "remove reservation",
        db.remove_reservation,
        restate.RunOptions[bool](),
        ctx.key(),
        req.trip_id,
        req.reservation_id,
    )
    if exists:
        # Removing the Porto hotel from a Lisbon & Porto trip can move its destination.
        await place_photo.request(ctx, [req.trip_id])
    return exists


@user.handler()
async def rename_trip(ctx: restate.ObjectContext, req: RenameTrip) -> None:
    await ctx.run_typed(
        "rename trip", db.rename_trip, restate.RunOptions[None](), ctx.key(), req.trip_id, req.name
    )


@user.handler()
async def delete_trip(ctx: restate.ObjectContext, req: TripRef) -> None:
    await ctx.run_typed("delete trip", db.delete_trip, restate.RunOptions[None](), ctx.key(), req.trip_id)
