"""`User` virtual object (key = email address): trip index, inbox and notifications.

All trip mutations go through here. Because exclusive handlers run one-at-a-time per user, two
confirmation emails arriving together can't both decide to create "the Lisbon trip".
"""

import restate
from pydantic import BaseModel, Field

from triptych import grouping
from triptych.models import (
    AddToTrip,
    FileRequest,
    FileResult,
    InboxItem,
    Notification,
    Overview,
    RemoveReservation,
    RenameTrip,
    TripSummary,
)
from triptych.services import trip as trip_svc

user = restate.VirtualObject("User")

MAX_INBOX = 50
MAX_NOTIFICATIONS = 50


class TripIndex(BaseModel):
    trips: dict[str, TripSummary] = Field(default_factory=dict)


class Inbox(BaseModel):
    items: list[InboxItem] = Field(default_factory=list)


class Notifications(BaseModel):
    items: list[Notification] = Field(default_factory=list)


async def _index(ctx: restate.ObjectContext | restate.ObjectSharedContext) -> TripIndex:
    return await ctx.get("trips", type_hint=TripIndex) or TripIndex()


def _store_summary(ctx: restate.ObjectContext, index: TripIndex, s: TripSummary) -> None:
    if s.reservation_count == 0:
        index.trips.pop(s.id, None)
        ctx.object_send(trip_svc.delete, s.id, None)
    else:
        index.trips[s.id] = s
    ctx.set("trips", index)


@user.handler()
async def file_reservations(ctx: restate.ObjectContext, req: FileRequest) -> FileResult:
    index = await _index(ctx)
    buckets = grouping.assign(req.reservations, list(index.trips.values()), lambda: str(ctx.uuid()))
    for b in buckets:
        moved = []
        for absorbed_id in b.absorbed:
            moved += (await ctx.object_call(trip_svc.get, absorbed_id, None)).reservations
            await ctx.object_call(trip_svc.delete, absorbed_id, None)
            index.trips.pop(absorbed_id, None)
        existing = index.trips.get(b.trip_id)
        name = existing.name if existing else grouping.trip_name(b.destination, b.start)
        summary = await ctx.object_call(
            trip_svc.add,
            b.trip_id,
            AddToTrip(
                owner=ctx.key(), name=name, destination=b.destination, reservations=moved + b.reservations
            ),
        )
        index.trips[summary.id] = summary
    ctx.set("trips", index)
    return FileResult(trip_ids=[b.trip_id for b in buckets])


@user.handler()
async def remove_reservation(ctx: restate.ObjectContext, req: RemoveReservation) -> None:
    index = await _index(ctx)
    if req.trip_id not in index.trips:
        raise restate.TerminalError("unknown trip", status_code=404)
    s = await ctx.object_call(trip_svc.remove_reservation, req.trip_id, req.reservation_id)
    _store_summary(ctx, index, s)


@user.handler()
async def rename_trip(ctx: restate.ObjectContext, req: RenameTrip) -> None:
    index = await _index(ctx)
    if req.trip_id not in index.trips:
        raise restate.TerminalError("unknown trip", status_code=404)
    s = await ctx.object_call(trip_svc.rename, req.trip_id, req.name)
    _store_summary(ctx, index, s)


@user.handler()
async def delete_trip(ctx: restate.ObjectContext, trip_id: str) -> None:
    index = await _index(ctx)
    if index.trips.pop(trip_id, None) is not None:
        ctx.object_send(trip_svc.delete, trip_id, None)
        ctx.set("trips", index)


@user.handler()
async def upsert_inbox(ctx: restate.ObjectContext, item: InboxItem) -> None:
    inbox = await ctx.get("inbox", type_hint=Inbox) or Inbox()
    others = [i for i in inbox.items if i.ingest_id != item.ingest_id]
    inbox.items = sorted([item, *others], key=lambda i: i.received_at, reverse=True)[:MAX_INBOX]
    ctx.set("inbox", inbox)


@user.handler()
async def notify(ctx: restate.ObjectContext, n: Notification) -> None:
    ns = await ctx.get("notifications", type_hint=Notifications) or Notifications()
    ns.items = [n, *ns.items][:MAX_NOTIFICATIONS]
    ctx.set("notifications", ns)


@user.handler()
async def dismiss_notifications(ctx: restate.ObjectContext) -> None:
    ctx.clear("notifications")


@user.handler(kind="shared")
async def overview(ctx: restate.ObjectSharedContext) -> Overview:
    index = await _index(ctx)
    inbox = await ctx.get("inbox", type_hint=Inbox) or Inbox()
    ns = await ctx.get("notifications", type_hint=Notifications) or Notifications()
    trips = sorted(index.trips.values(), key=lambda t: t.start_date)
    return Overview(
        trips=trips,
        inbox=inbox.items,
        notifications=ns.items,
    )
