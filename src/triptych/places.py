"""IATA location codes -> airport/station, city and country.

Backed by data/places.tsv, an extract of OpenTravelData (CC BY 4.0) regenerated with
scripts/update_places.py. Each place carries its IATA city codes, so LHR, LGW and St Pancras (QQS)
are all in LON, and JFK and EWR are both in NYC.
"""

import csv
from dataclasses import dataclass
from functools import cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "places.tsv"


@dataclass(frozen=True, slots=True)
class Place:
    iata: str
    kind: str  # "airport" | "rail"
    name: str
    city_codes: frozenset[str]
    city: str
    country_code: str
    country: str
    timezone: str


@cache
def _places() -> dict[str, Place]:
    with DATA.open(encoding="utf-8", newline="") as f:
        return {
            r["iata"]: Place(
                iata=r["iata"],
                kind=r["kind"],
                name=r["name"],
                city_codes=frozenset(r["city_codes"].split(",")),
                city=r["city"],
                country_code=r["country_code"],
                country=r["country"],
                timezone=r["timezone"],
            )
            for r in csv.DictReader(f, delimiter="\t")
        }


def lookup(code: str | None) -> Place | None:
    if not code:
        return None
    return _places().get(code.strip().upper())


@cache
def _city_timezones() -> dict[str, frozenset[str]]:
    zones: dict[str, set[str]] = {}
    for p in _places().values():
        zones.setdefault(p.city.casefold(), set()).add(p.timezone)
    return {city: frozenset(tz) for city, tz in zones.items()}


def city_timezone(city: str | None) -> str | None:
    """The timezone of a city named in English ("Beijing"), if every place we know by that name
    shares one. Paris doesn't (there's one in Texas), so gets none."""
    if not city:
        return None
    zones = _city_timezones().get(city.strip().casefold(), frozenset())
    return next(iter(zones)) if len(zones) == 1 else None


def same_city(a: str, b: str) -> bool:
    """Whether two IATA codes share a city code (LHR/LGW, JFK/EWR); unknown codes never do."""
    pa, pb = lookup(a), lookup(b)
    return bool(pa and pb and pa.city_codes & pb.city_codes)
