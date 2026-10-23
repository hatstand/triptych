"""Decide which trip each newly extracted reservation belongs to.

Pure and deterministic so it can run inside a Restate handler without a ctx.run and be unit-tested.
TripIt's rule of thumb is roughly "same dates ⇒ same trip"; we do the same with a day of slack so
that the flight home on check-out day, or a dinner the evening before a flight, lands in the trip.

Two refinements make the result independent of the order emails arrive in:
- Segments sharing a confirmation code (outbound + return of one booking) move as one unit.
- A unit that touches several trips merges them: e.g. outbound flight and return flight arrived
  first as two trips, then the hotel booking spanning the gap glues them together.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta

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


def pick_destination(rs: list[Reservation]) -> str | None:
    ordered = sorted(rs, key=lambda r: r.start_local)
    for kinds in ((Kind.LODGING,), (Kind.FLIGHT, Kind.RAIL), tuple(Kind)):
        for r in ordered:
            if r.kind in kinds and r.city:
                return r.city
    return None


def trip_name(destination: str | None, start: date) -> str:
    return f"{destination or 'Trip'}, {start:%B %Y}"


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
