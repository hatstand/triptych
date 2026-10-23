"""iCalendar export — subscribe to a user's feed from Google/Apple/Outlook calendars."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from icalendar import Calendar, Event

from triptych.models import Kind, Reservation, Trip

DEFAULT_DURATION = {
    Kind.RESTAURANT: timedelta(hours=2),
    Kind.ACTIVITY: timedelta(hours=2),
}


def _aware(local: datetime, tz: str | None) -> datetime:
    return local.replace(tzinfo=ZoneInfo(tz)) if tz else local


def _event(trip: Trip, r: Reservation) -> Event:
    ev = Event()
    ev.add("uid", f"{r.id}@triptych")
    ev.add("summary", r.title)
    start = _aware(r.start_local, r.start_tz)
    if r.end_local is not None:
        end = _aware(r.end_local, r.end_tz or r.start_tz)
    else:
        end = start + DEFAULT_DURATION.get(r.kind, timedelta(hours=1))
    if r.kind is Kind.LODGING:
        # A multi-night stay reads best as an all-day block.
        ev.add("dtstart", r.start_local.date())
        ev.add("dtend", (r.end_local or r.start_local).date() + timedelta(days=0 if r.end_local else 1))
    else:
        ev.add("dtstart", start)
        ev.add("dtend", end)
    loc = r.address or " → ".join(x for x in (r.start_location, r.end_location) if x)
    if loc:
        ev.add("location", loc)
    desc = [f"Trip: {trip.name}"]
    for label, val in (
        ("Provider", r.provider),
        ("Confirmation", r.confirmation_code),
        ("Flight", r.flight_number),
        ("Seat", r.seat),
        ("Notes", r.notes),
    ):
        if val:
            desc.append(f"{label}: {val}")
    ev.add("description", "\n".join(desc))
    return ev


def calendar(trips: list[Trip], name: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//triptych//EN")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", name)
    for t in trips:
        for r in t.reservations:
            cal.add_component(_event(t, r))
    return bytes(cal.to_ical())
