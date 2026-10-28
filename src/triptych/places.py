"""IATA location codes -> airport/station, city and country.

Backed by data/places.tsv, an extract of OpenTravelData (CC BY 4.0) regenerated with
scripts/update_places.py. Each place carries its IATA city codes, so LHR, LGW and St Pancras (QQS)
are all in LON, and JFK and EWR are both in NYC.
"""

import csv
import importlib.resources
import unicodedata
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
def _zone_countries() -> dict[str, frozenset[str]]:
    """Each canonical zone's countries, from zone.tab in the pinned tzdata package."""
    tab = importlib.resources.files("tzdata").joinpath("zoneinfo", "zone.tab").read_text()
    rows = (line.split("\t") for line in tab.splitlines() if line and not line.startswith("#"))
    out: dict[str, set[str]] = {}
    for row in rows:
        out.setdefault(row[2], set()).add(row[0])
    return {zone: frozenset(cs) for zone, cs in out.items()}


@cache
def timezones() -> tuple[str, ...]:
    """Every canonical IANA zone with a country: zone.tab from the pinned tzdata package, so the
    list (and the extraction schema built from it) is the same on every machine. Backward-compatible
    aliases (Asia/Calcutta, US/Eastern) and bare Etc/ offsets aren't in it."""
    return tuple(sorted(_zone_countries()))


def zone_in_country(zone: str, country_code: str | None) -> bool:
    """Whether `zone` is one of the country's (Atlantic/South_Georgia isn't Iceland's). True without
    a country, or for a zone zone.tab doesn't place."""
    countries = _zone_countries().get(zone)
    return not country_code or countries is None or country_code.strip().upper() in countries


@cache
def country_name(code: str) -> str | None:
    """A country's English name ("Iceland", "United Kingdom"), if we have places in it."""
    return next((p.country for p in _places().values() if p.country_code == code), None)


def _zone_file(name: str) -> bytes | None:
    try:
        return importlib.resources.files("tzdata").joinpath("zoneinfo", *name.split("/")).read_bytes()
    except OSError, ValueError:
        return None


@cache
def _canonical_by_file() -> dict[bytes, str]:
    out: dict[bytes, str] = {}
    for zone in timezones():
        if (data := _zone_file(zone)) is not None:
            out.setdefault(data, zone)
    return out


@cache
def canonical_timezone(name: str) -> str | None:
    """`name` as one of timezones(): itself, or for an alias (Asia/Chongqing, Europe/Kiev, US/Eastern)
    the canonical zone it links to (Asia/Shanghai, Europe/Kyiv, America/New_York). tzdata ships a
    link as a copy of its target's file, which is how it's found. None for a name that isn't a zone."""
    if name in timezones():
        return name
    if not name or ".." in name or name.startswith("/"):
        return None
    data = _zone_file(name)
    return _canonical_by_file().get(data) if data is not None else None


FOLDED = str.maketrans({"\u0131": "i", "ø": "o", "ß": "ss", "æ": "ae", "œ": "oe", "đ": "d", "ł": "l",
                        "þ": "th", "ð": "d", "ħ": "h"})  # fmt: skip
"""Letters that don't decompose into a base letter and an accent."""


def _fold(name: str) -> str:
    """A city name as it's matched: 'Malaga' finds Málaga, 'Reykjavik' Reykjavík, 'Izmir' İzmir."""
    decomposed = unicodedata.normalize("NFKD", name.strip().casefold())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).translate(FOLDED)


@cache
def _zones() -> tuple[dict[str, dict[str, frozenset[str]]], dict[str, str]]:
    """city name -> country -> zones of the places there; country -> its dominant zone."""
    by_name: dict[str, dict[str, set[str]]] = {}
    by_country: dict[str, Counter[str]] = {}
    for p in _places().values():
        by_name.setdefault(_fold(p.city), {}).setdefault(p.country_code, set()).add(p.timezone)
        by_country.setdefault(p.country_code, Counter())[p.timezone] += 1
    dominant = {}
    for country, zones in by_country.items():
        tz, n = zones.most_common(1)[0]
        if n >= COUNTRY_DOMINANCE * zones.total():
            dominant[country] = tz
    return {name: {c: frozenset(zs) for c, zs in cs.items()} for name, cs in by_name.items()}, dominant


def city_timezone(city: str | None, country_code: str | None = None) -> str | None:
    """The timezone of a city named in English ("Beijing"), with or without its accents, if every
    place we know by that name shares one. Paris doesn't (there's one in Texas) unless its country
    says which. With a country whose places don't include the city, it's the country's zone if one
    dominates."""
    by_name, dominant = _zones()
    countries = by_name.get(_fold(city or ""), {})
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
