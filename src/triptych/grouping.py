"""Decide which trip each newly extracted reservation belongs to.

Pure and deterministic so it can run inside a Restate handler without a ctx.run and be unit-tested.
TripIt's rule of thumb is roughly "same dates ⇒ same trip"; we do the same with a day of slack so
that the flight home on check-out day, or a dinner the evening before a flight, lands in the trip.

Two refinements make the result independent of the order emails arrive in:
- Segments sharing a confirmation code (outbound + return of one booking) move as one unit.
- A unit that touches several trips merges them: e.g. outbound flight and return flight arrived
  first as two trips, then the hotel booking spanning the gap glues them together.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise

from triptych import places
from triptych.journeys import MAX_LAYOVER, TRANSPORT, city_of
from triptych.models import Kind, Reservation, TripSummary

SLACK = timedelta(days=1)


@dataclass
class Bucket:
    trip_id: str
    is_new: bool
    start: date
    end: date
    destination: str | None
    reservations: list[Reservation] = field(default_factory=list)
    absorbed: list[str] = field(default_factory=list)
    """Existing trip ids merged into this one; their reservations must be moved here."""

    def gap_to(self, start: date, end: date) -> int:
        """Days between [start, end] and this bucket's range; 0 when they overlap."""
        if end < self.start:
            return (self.start - end).days
        if start > self.end:
            return (start - self.end).days
        return 0

    def absorb(self, other: "Bucket") -> None:
        self.reservations += other.reservations
        self.absorbed += other.absorbed + ([] if other.is_new else [other.trip_id])
        self.start = min(self.start, other.start)
        self.end = max(self.end, other.end)


def transport_stops(rs: list[Reservation]) -> list[str]:
    """Where a trip with no lodging stops, longest total stay first: each transport leg's arrival city,
    held until the next leg leaves. Connections (under MAX_LAYOVER), the final arrival and anywhere back
    in the departure city don't count, so LHR→PVG, PVG→SYD, SYD→PVG, PVG→LHR is Sydney, then Shanghai."""
    legs = sorted((r for r in rs if r.kind in TRANSPORT), key=lambda r: r.start_local)
    home = city_of(legs[0].start_location) if legs else None
    stays: dict[str, timedelta] = {}
    for leg, onward in pairwise(legs):
        held = onward.start_local - (leg.end_local or leg.start_local)
        if leg.city and held >= MAX_LAYOVER and " ".join(leg.city.casefold().split()) != home:
            stays[leg.city] = stays.get(leg.city, timedelta()) + held
    return sorted(stays, key=lambda city: stays[city], reverse=True)


def pick_destination(rs: list[Reservation]) -> str | None:
    ordered = sorted(rs, key=lambda r: r.start_local)
    if not any(r.kind is Kind.LODGING and r.city for r in ordered) and (stops := transport_stops(ordered)):
        return stops[0]
    for kinds in ((Kind.LODGING,), (Kind.FLIGHT, Kind.RAIL, Kind.FERRY), tuple(Kind)):
        for r in ordered:
            if r.kind in kinds and r.city:
                return r.city
    return None


# enrich.normalise writes resolved airports and stations as "Lisbon (LIS)".
_IATA_SUFFIX = re.compile(r"\(([A-Z]{3})\)$")


def place_query(rs: list[Reservation]) -> str | None:
    """What to look a trip's photo up by: its destination, and its country when an airport or station
    in that city says which ("Lisbon, Portugal"), so Portland, Maine isn't pictured as Oregon."""
    destination = pick_destination(rs)
    if destination is None:
        return None
    for r in rs:
        for location in (r.end_location, r.start_location):
            m = _IATA_SUFFIX.search(location or "")
            place = places.lookup(m.group(1)) if m else None
            if place and place.city.casefold() == destination.casefold() and place.country:
                return f"{destination}, {place.country}"
    return destination


MAX_TITLE_CITIES = 3


def trip_title(rs: list[Reservation]) -> str:
    """A name for a trip from everything in it: where you stay (else where the transport stops, else the
    destination) and when.
    "Lisbon, November 2026", "Lisbon & Porto, November 2026", "Tokyo, Kyoto & Osaka, March–April 2027".
    Recomputed as reservations are added, so it grows with the itinerary until the user renames it."""
    ordered = sorted(rs, key=lambda r: r.start_local)
    stays = list(dict.fromkeys(r.city for r in ordered if r.kind is Kind.LODGING and r.city))
    if not stays:
        stays = transport_stops(ordered)
    if not stays and (destination := pick_destination(ordered)):
        stays = [destination]
    if len(stays) > MAX_TITLE_CITIES:
        stays = [*stays[: MAX_TITLE_CITIES - 1], f"{len(stays) - MAX_TITLE_CITIES + 1} more"]
    where = " & ".join([", ".join(stays[:-1]), stays[-1]] if len(stays) > 1 else stays) or "Trip"
    start = min((r.start_date for r in ordered), default=None)
    end = max((r.end_date for r in ordered), default=None)
    if start is None or end is None:
        return where
    if (start.year, start.month) == (end.year, end.month):
        when = f"{start:%B %Y}"
    elif start.year == end.year:
        when = f"{start:%B}–{end:%B %Y}"
    else:
        when = f"{start:%B %Y}–{end:%B %Y}"
    return f"{where}, {when}"


def _units(new: list[Reservation]) -> list[list[Reservation]]:
    by_code: dict[str, list[Reservation]] = {}
    units: list[list[Reservation]] = []
    for r in sorted(new, key=lambda r: r.start_local):
        if r.confirmation_code:
            if r.confirmation_code not in by_code:
                by_code[r.confirmation_code] = []
                units.append(by_code[r.confirmation_code])
            by_code[r.confirmation_code].append(r)
        else:
            units.append([r])
    return units


def assign(
    new: list[Reservation], existing: list[TripSummary], new_trip_id: Callable[[], str]
) -> list[Bucket]:
    buckets = [
        Bucket(t.id, False, t.start_date, t.end_date, t.destination) for t in existing if t.reservation_count
    ]
    for unit in _units(new):
        start, end = min(r.start_date for r in unit), max(r.end_date for r in unit)
        near = [b for b in buckets if b.gap_to(start, end) <= SLACK.days]
        if not near:
            target = Bucket(new_trip_id(), True, start, end, None)
            buckets.append(target)
        else:
            # Survivor: an existing trip if any (keeps its id/name), else the earliest.
            target = min(near, key=lambda b: (b.is_new, b.start))
            for b in near:
                if b is not target:
                    target.absorb(b)
                    buckets.remove(b)
        target.reservations += unit
        target.start, target.end = min(target.start, start), max(target.end, end)

    touched = [b for b in buckets if b.reservations or b.absorbed]
    for b in touched:
        if b.is_new:
            b.destination = pick_destination(b.reservations)
    return touched
