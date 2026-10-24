"""IATA location codes -> airport/station, city and country.

Backed by data/places.tsv, an extract of OpenTravelData (CC BY 4.0) regenerated with
scripts/update_places.py. Each place carries its IATA city codes, so LHR, LGW and St Pancras (QQS)
are all in LON, and JFK and EWR are both in NYC.
"""

import csv
from collections import Counter
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


COUNTRY_DOMINANCE = 0.9
"""The share of a country's places that must sit in one zone for that to be the country's: China
(Asia/Urumqi in Xinjiang aside) and Great Britain qualify; the US, Portugal (the Azores) don't."""


@cache
def country_codes() -> tuple[str, ...]:
    """Every ISO 3166-1 alpha-2 code we have places in."""
    return tuple(sorted({p.country_code for p in _places().values()}))


@cache
def _zones() -> tuple[dict[str, dict[str, frozenset[str]]], dict[str, str]]:
    """city name -> country -> zones of the places there; country -> its dominant zone."""
    by_name: dict[str, dict[str, set[str]]] = {}
    by_country: dict[str, Counter[str]] = {}
    for p in _places().values():
        by_name.setdefault(p.city.casefold(), {}).setdefault(p.country_code, set()).add(p.timezone)
        by_country.setdefault(p.country_code, Counter())[p.timezone] += 1
    dominant = {}
    for country, zones in by_country.items():
        tz, n = zones.most_common(1)[0]
        if n >= COUNTRY_DOMINANCE * zones.total():
            dominant[country] = tz
    return {name: {c: frozenset(zs) for c, zs in cs.items()} for name, cs in by_name.items()}, dominant


def city_timezone(city: str | None, country_code: str | None = None) -> str | None:
    """The timezone of a city named in English ("Beijing"), if every place we know by that name
    shares one. Paris doesn't (there's one in Texas) unless its country says which. With a country
    whose places don't include the city, it's the country's zone if one dominates."""
    by_name, dominant = _zones()
    countries = by_name.get((city or "").strip().casefold(), {})
    country = (country_code or "").strip().upper()
    if country:
        zones = countries.get(country, frozenset())
        if not zones:
            return dominant.get(country)
    else:
        zones = frozenset[str]().union(*countries.values())
    return next(iter(zones)) if len(zones) == 1 else None


def same_city(a: str, b: str) -> bool:
    """Whether two IATA codes share a city code (LHR/LGW, JFK/EWR); unknown codes never do."""
    pa, pb = lookup(a), lookup(b)
    return bool(pa and pb and pa.city_codes & pb.city_codes)
