"""Group connecting flight/train/ferry segments into journeys for display.

Derived at render time from a trip's reservations; nothing is stored. Two consecutive transport
segments are one journey when all of these hold:
- the second departs from where the first arrives, or from another airport/station in the same
  city (LHR -> LGW, JFK -> EWR, Gare du Nord -> Gare de Lyon): see same_place,
- the layover is non-negative and under MAX_LAYOVER,
- no lodging in the layover city is checked into during the layover (a booked hotel makes it a
  stopover, not a connection).
"""

import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from triptych import places
from triptych.models import Kind, Reservation

MAX_LAYOVER = timedelta(hours=24)
TRANSPORT = frozenset({Kind.FLIGHT, Kind.RAIL, Kind.FERRY})

_IATA = re.compile(r"\(([A-Z]{3})\)\s*$")


def place_key(location: str | None) -> str | None:
    """What two locations must share to be the same airport/station."""
    if not location or not location.strip():
        return None
    if m := _IATA.search(location):
        return m.group(1)
    return " ".join(location.casefold().split())


def iata_of(location: str | None) -> str | None:
    """The code in an enriched location ('London (LGW)' -> 'LGW')."""
    m = _IATA.search(location) if location else None
    return m.group(1) if m else None


def city_of(location: str | None) -> str | None:
    """The city in an enriched location ('London (LGW)' -> 'london')."""
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

    With IATA codes on both sides, the city is the IATA city code (LHR/LGW are LON, JFK/EWR are
    NYC). Otherwise a plain station name counts as in a city when it starts with that city's
    name, taken from the other side's 'City (IATA)' form or the arriving leg's city.
    """
    here = place_key(a.end_location)
    if here is None or place_key(b.start_location) is None:
        return False
    if here == place_key(b.start_location):
        return True
    arrive_code, depart_code = iata_of(a.end_location), iata_of(b.start_location)
    if arrive_code and depart_code:
        return places.same_city(arrive_code, depart_code)
    arrive_city = city_of(a.end_location) or (" ".join(a.city.casefold().split()) if a.city else None)
    depart_city = city_of(b.start_location)
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
        nouns = {
            frozenset({Kind.FLIGHT}): "Airport change",
            frozenset({Kind.RAIL}): "Station change",
            frozenset({Kind.FERRY}): "Port change",
        }
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
        """'2 flights', '3 trains', '2 ferries', or '2 segments' when mixed."""
        kinds = {r.kind for r in self.legs}
        nouns = {
            frozenset({Kind.FLIGHT}): "flights",
            frozenset({Kind.RAIL}): "trains",
            frozenset({Kind.FERRY}): "ferries",
        }
        noun = nouns.get(frozenset(kinds), "segments")
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
    # A check-in time is only when the hotel opens its doors, so one falling in the layover only
    # makes a stopover if the hotel could be in the layover city.
    here = a.city or city_of(a.end_location)
    return not any(a.end_local <= h.start_local < b.start_local and _maybe_in(h, here) for h in lodging)


@dataclass(frozen=True, slots=True)
class Stay:
    """One end of a lodging reservation: its check-in, or its check-out on a later day.

    A hotel's check-in time is the earliest it allows and its check-out time the latest, so neither
    says when you actually arrive or leave: see timeline for how they are ordered.
    """

    hotel: Reservation
    checkout: bool = False

    @property
    def kind(self) -> Kind:
        return Kind.LODGING

    @property
    def time(self) -> datetime:
        """The hotel's own check-in (from) or check-out (by) time."""
        if self.checkout:
            assert self.hotel.end_local is not None
            return self.hotel.end_local
        return self.hotel.start_local

    @property
    def tz(self) -> str | None:
        return (self.hotel.end_tz or self.hotel.start_tz) if self.checkout else self.hotel.start_tz

    @property
    def date(self) -> date:
        return self.time.date()

    @property
    def nights(self) -> int:
        return (self.hotel.end_date - self.hotel.start_date).days


Item = Reservation | Journey | Stay


def instant(t: datetime, tz: str | None) -> datetime:
    """A wall-clock time as naive UTC, for ordering times in different zones (as-is with no zone)."""
    a = _aware(t, tz)
    return a.astimezone(UTC).replace(tzinfo=None) if a else t


def _zones(ordered: list[Reservation]) -> dict[str, tuple[str | None, str | None]]:
    """Each reservation's start and end zones, taking a missing one from where the trip is by then
    (the last known zone), so a dinner with no zone still sorts against flights that have one."""
    here: str | None = None
    zones: dict[str, tuple[str | None, str | None]] = {}
    for r in ordered:
        start = r.start_tz or here
        end = r.end_tz or (start if r.kind not in TRANSPORT else None) or here
        zones[r.id] = (start, end)
        here = r.end_tz or r.start_tz or here
    return zones


def _maybe_in(hotel: Reservation, city: str | None) -> bool:
    """Whether a transport leg arriving in or leaving from `city` could be the hotel's: unless both
    cities are known and differ (the leg out to Sydney after a stopover in Saigon isn't)."""
    if not hotel.city or not city:
        return True
    return " ".join(hotel.city.casefold().split()) == " ".join(city.casefold().split())


def timeline(reservations: list[Reservation]) -> list[tuple[date, list[Item]]]:
    """The trip by day, in the order things happen.

    Each run of connecting transport segments is one Journey, and each hotel stay is a check-in on
    its first day and a check-out on its last. A hotel's times are what it allows, not when you get
    there, so a check-in sorts after any transport arriving that day (check-in from 14:00, train in
    at 18:00: the hotel comes after the train), and a check-out before any departing that day,
    counting only transport to or from the hotel's city when both cities are known.
    Times in different zones are compared as instants.
    """
    ordered = sorted(reservations, key=lambda r: r.start_local)
    lodging = [r for r in ordered if r.kind == Kind.LODGING]
    transport = [r for r in ordered if r.kind in TRANSPORT]
    zones = _zones(ordered)
    chains: dict[str, list[Reservation]] = {}  # first leg id -> legs
    current: list[Reservation] = []
    for r in transport:
        if current and connects(current[-1], r, lodging):
            current.append(r)
        else:
            current = [r]
            chains[r.id] = current

    def starts(r: Reservation) -> datetime:
        return instant(r.start_local, zones[r.id][0])

    def ends(r: Reservation) -> datetime:
        assert r.end_local is not None
        return instant(r.end_local, zones[r.id][1])

    def check_in(s: Stay) -> datetime:
        arrivals = [
            ends(t)
            for t in transport
            if t.end_local
            and t.end_local.date() == s.date
            and _maybe_in(s.hotel, t.city or city_of(t.end_location))
        ]
        return max([starts(s.hotel), *arrivals])

    def check_out(s: Stay) -> datetime:
        departures = [
            starts(t)
            for t in transport
            if t.start_date == s.date and _maybe_in(s.hotel, city_of(t.start_location))
        ]
        return min([ends(s.hotel), *departures])

    in_journey = {r.id for legs in chains.values() if len(legs) > 1 for r in legs}
    # (day, effective time, tie-break, item): a check-in follows an arrival at the same minute and a
    # check-out precedes a departure.
    entries: list[tuple[date, datetime, int, Item]] = []
    for r in ordered:
        legs = chains.get(r.id)
        if legs and len(legs) > 1:
            entries.append((r.start_date, starts(r), 0, Journey(legs)))
        elif r.id in in_journey:
            continue
        elif r.kind == Kind.LODGING:
            stay = Stay(r)
            entries.append((stay.date, check_in(stay), 1, stay))
            if r.end_local and r.end_date > r.start_date:
                leave = Stay(r, checkout=True)
                entries.append((leave.date, check_out(leave), -1, leave))
        else:
            entries.append((r.start_date, starts(r), 0, r))
    entries.sort(key=lambda e: e[:3])

    days: list[tuple[date, list[Item]]] = []
    for day, _, _, item in entries:
        if days and days[-1][0] == day:
            days[-1][1].append(item)
        else:
            days.append((day, [item]))
    return days


def tz_abbr(t: datetime, tz: str | None) -> str:
    """The zone's abbreviation at that time ('GMT', 'WEST', '+07'), or '' when unknown."""
    a = _aware(t, tz)
    return (a.tzname() or "") if a else ""


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
