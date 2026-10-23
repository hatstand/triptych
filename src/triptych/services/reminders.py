"""`Reminders` service: durable check-in / leave-for-the-airport nudges.

Filing schedules a delayed send per reminder; Restate holds the timer. When it fires we re-check
Postgres, so reminders for deleted or rescheduled segments are dropped rather than cancelled.
"""

from datetime import UTC, datetime, time, timedelta

import restate

from triptych import db
from triptych.db import FiledReservation
from triptych.enrich import to_utc
from triptych.models import Kind, Notification, Reminder, Reservation

reminders = restate.Service("Reminders")


def reminder_times(r: Reservation) -> list[tuple[datetime, str]]:
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


async def schedule(ctx: restate.Context, owner: str, filed: list[FiledReservation]) -> None:
    """Idempotent: called for every reservation in a touched trip, and Restate dedupes each
    (reservation, fire time) via the idempotency key. A rescheduled segment gets a new key; its old
    reminder fires, finds the start time changed, and does nothing."""
    now = datetime.fromtimestamp(await ctx.time(), UTC)
    for f in filed:
        r = f.reservation
        for fire_at, text in reminder_times(r):
            if fire_at > now:
                ctx.service_send(
                    fire,
                    Reminder(
                        owner=owner,
                        trip_id=f.trip_id,
                        reservation_id=r.id,
                        expected_start=r.start_local,
                        text=text,
                    ),
                    send_delay=fire_at - now,
                    idempotency_key=f"reminder:{r.id}:{fire_at.isoformat()}",
                )


@reminders.handler()
async def fire(ctx: restate.Context, r: Reminder) -> None:
    target = await ctx.run_typed(
        "check reservation",
        db.reminder_target,
        restate.RunOptions[db.ReminderTarget](),
        r.reservation_id,
        r.expected_start,
    )
    if target.owner is None:
        return
    at = datetime.fromtimestamp(await ctx.time(), UTC)
    n = Notification(id=str(ctx.uuid()), at=at, trip_id=target.trip_id, text=r.text)
    await ctx.run_typed("notify", db.add_notification, restate.RunOptions[None](), target.owner, n)
