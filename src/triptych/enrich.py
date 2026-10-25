"""Deterministic clean-up of LLM output: parse times, resolve airports to cities and timezones."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from triptych import places
from triptych.journeys import iata_of
from triptych.models import ExtractedReservation, Kind, Reservation

TRANSPORT = (Kind.FLIGHT, Kind.RAIL)


def valid_tz(name: str | None) -> str | None:
    if not name:
        return None
    try:
        ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return None
    return name


def parse_local(value: str | None) -> datetime | None:
    if not value:
        return None
    v = value.strip().replace(" ", "T")
    try:
        dt = datetime.fromisoformat(v)
    except ValueError:
        return None
    # We store wall-clock time plus a separate tz name; drop any offset the model added.
    return dt.replace(tzinfo=None, second=0, microsecond=0)


def to_utc(local: datetime | None, tz: str | None) -> datetime | None:
    if local is None or tz is None:
        return None
    return local.replace(tzinfo=ZoneInfo(tz)).astimezone(ZoneInfo("UTC"))


def normalise(
    x: ExtractedReservation, *, reservation_id: str, ingest_id: str, run_id: str | None = None
) -> Reservation | None:
    start = parse_local(x.start_local)
    if start is None:
        return None
    end = parse_local(x.end_local)

    # Our places data names a city's zone more reliably than the model does (it once put a Shanghai
    # hotel in Asia/Hong_Kong); the model's own guesses are the fallback.
    city_tz = places.city_timezone(x.city, x.country_code)
    start_tz, end_tz = valid_tz(x.start_timezone), city_tz or valid_tz(x.end_timezone)
    city = x.city
    start_location, end_location = x.start_location, x.end_location

    if x.kind in TRANSPORT:
        # `city` is where transport arrives. It departs from the airport's zone, or start_city's, or
        # the model's start_timezone, or else wherever the previous leg landed (fill_timezones).
        start_tz = places.city_timezone(x.start_city, x.start_country_code) or start_tz
        dep = places.lookup(x.start_iata or iata_of(x.start_location))
        arr = places.lookup(x.end_iata or iata_of(x.end_location))
        if dep:
            start_tz = valid_tz(dep.timezone) or start_tz
            start_location = f"{dep.city} ({dep.iata})"
        if arr:
            end_tz = valid_tz(arr.timezone) or end_tz
            end_location = f"{arr.city} ({arr.iata})"
            city = arr.city
    else:
        # Anything else happens in its city; failing that, if the model gave one zone, it's both.
        start_tz = city_tz or start_tz or end_tz
        end_tz = end_tz or start_tz

    # Guard against obviously inverted ranges (e.g. overnight flight with the arrival date omitted).
    if end is not None and end < start and x.kind is not Kind.LODGING:
        end = end + timedelta(days=1)

    return Reservation(
        id=reservation_id,
        kind=x.kind,
        title=x.title.strip() or x.kind.value.title(),
        provider=x.provider,
        confirmation_code=x.confirmation_code,
        start_local=start,
        start_tz=start_tz,
        end_local=end,
        end_tz=end_tz,
        start_location=start_location,
        end_location=end_location,
        address=x.address,
        city=city,
        service_number=x.service_number,
        seat=x.seat,
        travelers=x.travelers,
        notes=x.notes,
        source_ingest_id=ingest_id,
        source_run_id=run_id,
    )


STILL_THERE = timedelta(days=1)
"""How long after landing a transport leg with no departure zone is taken to leave from where the
previous one arrived. Later than that there may be legs we haven't been sent in between (a week
after landing in Hong Kong, a train from Chengdu), so its own arrival zone is the better guess."""


def fill_timezones(rs: list[Reservation]) -> None:
    """Fill in timezones the document didn't give. Hotels and restaurants rarely state one, and
    trains have no airport codes to look up, but the leg that got you there says where you are.

    A flight or train whose locations name a known airport or station ('London (LHR)') takes its
    zones. A hotel/restaurant in a city whose name is unambiguous takes that city's zone, even over the
    model's (which once put a Shanghai hotel in Asia/Hong_Kong), as does a transport leg with no
    arrival zone. Failing that a hotel/restaurant takes the arrival tz of the latest earlier
    transport leg (or the departure tz of the next one). A transport leg with no departure zone
    leaves from where the previous one landed if that was within STILL_THERE, and otherwise from
    the zone it arrives in. `rs` must be sorted by start time; mutated in place."""
    for r in rs:
        if r.kind in TRANSPORT:
            # An airport's zone beats any guess, made here or by the model.
            if dep := places.lookup(iata_of(r.start_location)):
                r.start_tz = dep.timezone
            if arr := places.lookup(iata_of(r.end_location)):
                r.end_tz = arr.timezone
            # A transport leg's city is where it arrives.
            r.end_tz = r.end_tz or places.city_timezone(r.city)
        elif tz := places.city_timezone(r.city):
            r.start_tz = r.end_tz = tz
    arrival: str | None = None
    landed: datetime | None = None
    for r in rs:
        if r.kind in TRANSPORT:
            recent = landed is not None and r.start_local - landed <= STILL_THERE
            r.start_tz = r.start_tz or (arrival if recent else None) or r.end_tz or arrival
            r.end_tz = r.end_tz or r.start_tz
            if r.end_tz:
                arrival, landed = r.end_tz, r.end_local or r.start_local
        elif r.start_tz is None and arrival:
            r.start_tz = r.end_tz = arrival
    departure: str | None = None
    for r in reversed(rs):
        if r.kind in TRANSPORT:
            if r.start_tz is None:
                r.start_tz = r.end_tz = departure
            departure = r.start_tz or departure
        elif r.start_tz is None and departure:
            r.start_tz = r.end_tz = departure
