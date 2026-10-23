"""Group connecting flight/train segments into journeys for display.

Derived at render time from a trip's reservations; nothing is stored. Two consecutive transport
segments are one journey when all of these hold:
- the second departs from where the first arrives, or from another airport/station in the same
  city (LHR -> LGW, Gare du Nord -> Gare de Lyon): see same_place,
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


def city_of(location: str | None) -> str | None:
    """The city in an enriched airport location ('London (LGW)' -> 'london')."""
    if not location or not (m := _IATA.search(location)):
        return None
    return " ".join(location[: m.start()].casefold().split()) or None


def _starts_with_city(location: str | None, city: str | None) -> bool:
    if not location or not city:
        return False
    words, city_words = location.casefold().split(), city.split()
    return words[: len(city_words)] == city_words


def same_place(a: Reservation, b: Reservation) -> bool:
    """Whether b departs from where a arrives, allowing a change of airport/station in one city.

    Cities come from the enriched 'City (IATA)' form, falling back to the arriving leg's city; a
    plain station name counts as in a city when it starts with that city's name.
    """
    here = place_key(a.end_location)
    if here is None or place_key(b.start_location) is None:
        return False
    if here == place_key(b.start_location):
        return True
    arrive_city = city_of(a.end_location) or (" ".join(a.city.casefold().split()) if a.city else None)
    depart_city = city_of(b.start_location)
    if arrive_city and depart_city:
        return arrive_city == depart_city
    return _starts_with_city(b.start_location, arrive_city) or _starts_with_city(a.end_location, depart_city)


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
class Connection:
    arrive: str
    depart: str
    wait: timedelta
    changes: bool
    """Whether this connection moves to a different airport/station."""
    kinds: frozenset[Kind]

    @property
    def warning(self) -> str | None:
        """Shown prominently: a change of airport/station needs ground transfer time."""
        if not self.changes:
            return None
        nouns = {frozenset({Kind.FLIGHT}): "Airport change", frozenset({Kind.RAIL}): "Station change"}
        noun = nouns.get(self.kinds, "Transfer")
        return f"{noun}: {self.arrive} → {self.depart}, {format_duration(self.wait)} to make it"

    @property
    def label(self) -> str:
        return f"{self.arrive}/{self.depart}" if self.changes else self.arrive

    @property
    def where(self) -> str:
        """'in SGN', or 'changing LHR → LGW' when the airport/station changes."""
        return f"changing {self.arrive} → {self.depart}" if self.changes else f"in {self.arrive}"


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
        """Route labels, with 'LHR/LGW' where a change of airport/station happens."""
        first, last = self.legs[0], self.legs[-1]
        return (
            [place_label(first.start_location)]
            + [c.label for c in self.connections]
            + [place_label(last.end_location)]
        )

    @property
    def layovers(self) -> list[timedelta]:
        return [c.wait for c in self.connections]

    @property
    def connections(self) -> list["Connection"]:
        return [
            Connection(
                place_label(a.end_location),
                place_label(b.start_location),
                layover(a, b),
                place_key(a.end_location) != place_key(b.start_location),
                frozenset({a.kind, b.kind}),
            )
            for a, b in zip(self.legs, self.legs[1:], strict=False)
        ]

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
    if not same_place(a, b):
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
