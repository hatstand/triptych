"""Deterministic clean-up of LLM output: parse times, resolve airports to cities and timezones."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from triptych import places
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


def normalise(x: ExtractedReservation, *, reservation_id: str, ingest_id: str) -> Reservation | None:
    start = parse_local(x.start_local)
    if start is None:
        return None
    end = parse_local(x.end_local)

    start_tz = end_tz = valid_tz(x.timezone)
    city = x.city
    start_location, end_location = x.start_location, x.end_location

    if x.kind in TRANSPORT:
        # The model's timezone is its city's, which for transport is where it arrives; where it
        # departs from is the airport's, or else filled in from the previous leg (fill_timezones).
        start_tz = None
        dep, arr = places.lookup(x.start_iata), places.lookup(x.end_iata)
        if dep:
            start_tz = valid_tz(dep.timezone)
            start_location = f"{dep.city} ({dep.iata})"
        if arr:
            end_tz = valid_tz(arr.timezone) or end_tz
            end_location = f"{arr.city} ({arr.iata})"
            city = arr.city

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
        flight_number=x.flight_number,
        seat=x.seat,
        travelers=x.travelers,
        notes=x.notes,
        source_ingest_id=ingest_id,
    )


def fill_timezones(rs: list[Reservation]) -> None:
    """Fill in timezones the document didn't give. Hotels and restaurants rarely state one, and
    trains have no airport codes to look up, but the leg that got you there says where you are.

    A reservation with no timezone takes its city's, if that name is unambiguous. Failing that a
    hotel/restaurant takes the arrival tz of the latest earlier transport leg (or the departure tz
    of the next one), and a transport leg departs from wherever the previous one arrived and
    arrives in the same zone unless told otherwise. `rs` must be sorted by start time; mutated in
    place."""
    for r in rs:
        if r.kind in TRANSPORT:
            # A transport leg's city is where it arrives.
            r.end_tz = r.end_tz or places.city_timezone(r.city)
        elif r.start_tz is None:
            r.start_tz = r.end_tz = places.city_timezone(r.city)
    arrival: str | None = None
    for r in rs:
        if r.kind in TRANSPORT:
            r.start_tz = r.start_tz or arrival or r.end_tz
            r.end_tz = r.end_tz or r.start_tz
            arrival = r.end_tz or arrival
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
