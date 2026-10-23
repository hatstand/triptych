"""`Trip` virtual object (key = trip id): the itinerary itself, plus durable reminders.

Mutations arrive only via the owning `User` object, which keeps its trip index consistent; the web
app may call the shared `get`/`ask` handlers directly.
"""

from datetime import UTC, datetime, time, timedelta

import restate

from triptych.enrich import fill_timezones, to_utc
from triptych.models import (
    AddToTrip,
    Answer,
    Kind,
    LlmAskRequest,
    Notification,
    Question,
    Reminder,
    Reservation,
    Trip,
    TripSummary,
)
from triptych.services.llm_service import GPU_SCOPE, limit_key
from triptych.services.llm_service import ask as llm_ask

trip = restate.VirtualObject("Trip")

STATE = "trip"


async def _load(ctx: restate.ObjectContext | restate.ObjectSharedContext) -> Trip:
    t = await ctx.get(STATE, type_hint=Trip)
    if t is None:
        raise restate.TerminalError(f"trip {ctx.key()} not found", status_code=404)
    return t


def _dedupe_key(r: Reservation) -> tuple[str, str, str, str]:
    return (r.kind.value, r.confirmation_code or "", r.flight_number or r.title, r.start_date.isoformat())


def _reminders(r: Reservation) -> list[tuple[datetime, str]]:
    """(UTC fire time, text) pairs for a reservation."""
    out: list[tuple[datetime, str]] = []
    if r.kind is Kind.FLIGHT:
        dep = to_utc(r.start_local, r.start_tz)
        if dep is not None:
            out.append((dep - timedelta(hours=24), f"Check-in is open for {r.title}."))
            out.append(
                (
                    dep - timedelta(hours=3),
                    f"Time to head to the airport: {r.title} departs at {r.start_local:%H:%M}.",
                )
            )
    elif r.kind is Kind.LODGING:
        morning = to_utc(datetime.combine(r.start_date, time(9, 0)), r.start_tz)
        if morning is not None:
            out.append((morning, f"Checking in today at {r.title} (from {r.start_local:%H:%M})."))
    return out


@trip.handler()
async def add(ctx: restate.ObjectContext, req: AddToTrip) -> TripSummary:
    current = await ctx.get(STATE, type_hint=Trip) or Trip(
        id=ctx.key(), owner=req.owner, name=req.name, destination=req.destination
    )
    if current.owner != req.owner:
        raise restate.TerminalError("trip belongs to another user", status_code=403)

    by_key = {_dedupe_key(r): r for r in current.reservations}
    for r in req.reservations:
        by_key[_dedupe_key(r)] = r  # a re-sent/updated confirmation replaces the old segment
    current.reservations = sorted(by_key.values(), key=lambda r: r.start_local)
    fill_timezones(current.reservations)
    ctx.set(STATE, current)

    now = datetime.fromtimestamp(await ctx.time(), UTC)
    for r in req.reservations:
        for fire_at, text in _reminders(r):
            if fire_at > now:
                ctx.object_send(
                    remind,
                    ctx.key(),
                    Reminder(reservation_id=r.id, expected_start=r.start_local, text=text),
                    send_delay=fire_at - now,
                )
    return current.summary()


@trip.handler(kind="shared")
async def get(ctx: restate.ObjectSharedContext) -> Trip:
    return await _load(ctx)


@trip.handler()
async def remove_reservation(ctx: restate.ObjectContext, reservation_id: str) -> TripSummary:
    t = await _load(ctx)
    t.reservations = [r for r in t.reservations if r.id != reservation_id]
    ctx.set(STATE, t)
    return t.summary()


@trip.handler()
async def rename(ctx: restate.ObjectContext, name: str) -> TripSummary:
    t = await _load(ctx)
    t.name = name.strip() or t.name
    ctx.set(STATE, t)
    return t.summary()


@trip.handler()
async def delete(ctx: restate.ObjectContext) -> None:
    ctx.clear_all()


@trip.handler()
async def remind(ctx: restate.ObjectContext, r: Reminder) -> None:
    """Fired by a delayed send. Stale reminders (segment deleted/rescheduled) are dropped."""
    t = await ctx.get(STATE, type_hint=Trip)
    if t is None or not any(
        x.id == r.reservation_id and x.start_local == r.expected_start for x in t.reservations
    ):
        return
    from triptych.services.user import notify  # circular: User calls Trip

    ctx.object_send(
        notify,
        t.owner,
        Notification(
            id=str(ctx.uuid()), at=datetime.fromtimestamp(await ctx.time(), UTC), trip_id=t.id, text=r.text
        ),
    )


def itinerary_text(t: Trip) -> str:
    lines = [f"Trip: {t.name}"]
    for r in t.reservations:
        when = f"{r.start_local:%a %d %b %Y %H:%M}"
        if r.end_local:
            when += f" → {r.end_local:%a %d %b %H:%M}"
        bits = [r.kind.value.upper(), r.title, when]
        for label, val in (
            ("provider", r.provider),
            ("confirmation", r.confirmation_code),
            ("from", r.start_location),
            ("to", r.end_location),
            ("address", r.address),
            ("seat", r.seat),
            ("notes", r.notes),
        ):
            if val:
                bits.append(f"{label}: {val}")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


@trip.handler(kind="shared")
async def ask(ctx: restate.ObjectSharedContext, q: Question) -> Answer:
    t = await _load(ctx)
    return await ctx.scope(GPU_SCOPE).service_call(
        llm_ask,
        LlmAskRequest(context=itinerary_text(t), question=q.text),
        limit_key=limit_key("interactive", t.owner),
    )
