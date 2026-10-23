"""Group connecting flight/train segments into journeys for display.

Derived at render time from a trip's reservations; nothing is stored. Two consecutive transport
segments are one journey when all of these hold:
- the second departs from where the first arrives (IATA code, else the location text),
- the layover is non-negative and under MAX_LAYOVER,
- no lodging is checked into during the layover (a booked hotel makes it a stopover, not a
  connection).
"""

import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from triptych.models import Kind, Reservation

MAX_LAYOVER = timedelta(hours=24)
TRANSPORT = frozenset({Kind.FLIGHT, Kind.RAIL})

_IATA = re.compile(r"\(([A-Z]{3})\)\s*$")


def place_key(location: str | None) -> str | None:
    """What two locations must share to be the same airport/station."""
    if not location or not location.strip():
        return None
    if m := _IATA.search(location):
        return m.group(1)
    return " ".join(location.casefold().split())


def place_label(location: str | None) -> str:
    """Short label for a journey's route line: the IATA code when known."""
    if not location:
        return "?"
    m = _IATA.search(location)
    return m.group(1) if m else location


def _aware(t: datetime, tz: str | None) -> datetime | None:
    if not tz:
        return None
    try:
        return t.replace(tzinfo=ZoneInfo(tz))
    except (ZoneInfoNotFoundError, ValueError):
        return None


def elapsed(start: datetime, start_tz: str | None, end: datetime, end_tz: str | None) -> timedelta:
    """end - start, across time zones when both are known (otherwise as local wall-clock times)."""
    a, b = _aware(start, start_tz), _aware(end, end_tz)
    return b - a if a and b else end - start


@dataclass(frozen=True, slots=True)
class Journey:
    legs: list[Reservation]

    @property
    def kind(self) -> Kind:
        return self.legs[0].kind

    @property
    def summary(self) -> str:
        """'2 flights', '3 trains', or '2 segments' when mixed."""
        kinds = {r.kind for r in self.legs}
        noun = {frozenset({Kind.FLIGHT}): "flights", frozenset({Kind.RAIL}): "trains"}.get(
            frozenset(kinds), "segments"
        )
        return f"{len(self.legs)} {noun}"

    @property
    def start_date(self) -> date:
        return self.legs[0].start_date

    @property
    def stops(self) -> list[str]:
        return [place_label(self.legs[0].start_location)] + [place_label(r.end_location) for r in self.legs]

    @property
    def layovers(self) -> list[timedelta]:
        return [layover(a, b) for a, b in zip(self.legs, self.legs[1:], strict=False)]

    @property
    def connections(self) -> list[tuple[str, timedelta]]:
        """(where, how long) for each change between legs."""
        return list(zip(self.stops[1:-1], self.layovers, strict=True))

    @property
    def duration(self) -> timedelta:
        first, last = self.legs[0], self.legs[-1]
        assert last.end_local is not None
        return elapsed(first.start_local, first.start_tz, last.end_local, last.end_tz)


def layover(a: Reservation, b: Reservation) -> timedelta:
    assert a.end_local is not None
    return elapsed(a.end_local, a.end_tz, b.start_local, b.start_tz)


def connects(a: Reservation, b: Reservation, lodging: list[Reservation]) -> bool:
    if a.end_local is None or b.end_local is None:
        return False
    here = place_key(a.end_location)
    if here is None or here != place_key(b.start_location):
        return False
    if not timedelta(0) <= layover(a, b) < MAX_LAYOVER:
        return False
    return not any(a.end_local <= h.start_local < b.start_local for h in lodging)


def timeline(reservations: list[Reservation]) -> list[tuple[date, list[Reservation | Journey]]]:
    """Reservations by start day, with each run of connecting transport segments as one Journey."""
    ordered = sorted(reservations, key=lambda r: r.start_local)
    lodging = [r for r in ordered if r.kind == Kind.LODGING]
    chains: dict[str, list[Reservation]] = {}  # first leg id -> legs
    current: list[Reservation] = []
    for r in (r for r in ordered if r.kind in TRANSPORT):
        if current and connects(current[-1], r, lodging):
            current.append(r)
        else:
            current = [r]
            chains[r.id] = current

    in_journey = {r.id for legs in chains.values() if len(legs) > 1 for r in legs}
    items: list[Reservation | Journey] = []
    for r in ordered:
        legs = chains.get(r.id)
        if legs and len(legs) > 1:
            items.append(Journey(legs))
        elif r.id not in in_journey:
            items.append(r)

    days: list[tuple[date, list[Reservation | Journey]]] = []
    for item in items:
        if days and days[-1][0] == item.start_date:
            days[-1][1].append(item)
        else:
            days.append((item.start_date, [item]))
    return days


def format_duration(d: timedelta) -> str:
    """'45m', '2h 05m', '1d 3h' — for layovers and journey totals."""
    minutes = max(0, round(d.total_seconds() / 60))
    days, minutes = divmod(minutes, 24 * 60)
    hours, minutes = divmod(minutes, 60)
    if days:
        return f"{days}d {hours}h"
    if hours:
        return f"{hours}h {minutes:02d}m"
    return f"{minutes}m"
