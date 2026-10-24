import base64
from datetime import UTC, datetime
from pathlib import Path

from triptych import enrich, parsing
from triptych.ical import calendar
from triptych.models import ExtractedReservation, IngestRequest, IngestSource, Kind, Reservation, Trip

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def ingest(name: str, source: IngestSource = "email") -> IngestRequest:
    return IngestRequest(
        user="me@example.com",
        source=source,
        data_b64=base64.b64encode((SAMPLES / name).read_bytes()).decode(),
        received_at=datetime(2026, 10, 23, tzinfo=UTC),
    )


def test_parse_plain_email() -> None:
    doc = parsing.parse(ingest("01-flight-lisbon.eml"))
    assert doc.subject and "QZ7K2M" in doc.subject
    assert "AQ 236" in doc.text


def test_parse_html_email_strips_markup() -> None:
    doc = parsing.parse(ingest("02-hotel-lisbon.eml"))
    assert "HX55821" in doc.text
    assert "<td>" not in doc.text


def test_pasted_email_is_detected() -> None:
    doc = parsing.parse(ingest("04-rail-paris.eml", source="paste"))
    assert doc.subject and "St Pancras" in doc.subject
    assert "Message-ID" not in doc.text


def test_normalise_flight_uses_airport_timezones() -> None:
    x = ExtractedReservation(
        kind=Kind.FLIGHT,
        title="AQ 236",
        start_local="2026-11-12T15:40",
        end_local="2026-11-13T11:25",
        start_iata="SFO",
        end_iata="lis",
        flight_number="AQ 236",
    )
    r = enrich.normalise(x, reservation_id="r1", ingest_id="i1")
    assert r is not None
    assert (r.start_tz, r.end_tz) == ("America/Los_Angeles", "Europe/Lisbon")
    assert r.city == "Lisbon"
    assert enrich.to_utc(r.start_local, r.start_tz) == datetime(2026, 11, 12, 23, 40, tzinfo=UTC)


def test_normalise_rejects_unparseable_time_and_bad_tz() -> None:
    bad = ExtractedReservation(kind=Kind.OTHER, title="?", start_local="sometime next week")
    assert enrich.normalise(bad, reservation_id="r", ingest_id="i") is None
    x = ExtractedReservation(
        kind=Kind.ACTIVITY, title="Tour", start_local="2026-11-14 10:00", timezone="Mars/Olympus"
    )
    r = enrich.normalise(x, reservation_id="r", ingest_id="i")
    assert r is not None and r.start_tz is None


def test_ical_export() -> None:
    x = ExtractedReservation(
        kind=Kind.LODGING,
        title="Casa",
        start_local="2026-11-13T15:00",
        end_local="2026-11-17T11:00",
        timezone="Europe/Lisbon",
    )
    r = enrich.normalise(x, reservation_id="r1", ingest_id="i1")
    assert r is not None
    ics = calendar([Trip(id="t", owner="me", name="Lisbon", reservations=[r])], "test").decode()
    assert "DTSTART;VALUE=DATE:20261113" in ics and "UID:r1@triptych" in ics


def test_fill_timezones_from_arrival_flight() -> None:
    def r(kind: Kind, start: str, tz: str | None = None, end_tz: str | None = None) -> Reservation:
        return Reservation(
            id=start,
            kind=kind,
            title=kind.value,
            start_local=datetime.fromisoformat(start),
            start_tz=tz,
            end_tz=end_tz,
        )

    pre = r(Kind.RESTAURANT, "2026-11-11T19:00")
    out = r(Kind.FLIGHT, "2026-11-12T15:40", "America/Los_Angeles", "Europe/Lisbon")
    hotel = r(Kind.LODGING, "2026-11-13T15:00")
    rs = [pre, out, hotel]
    enrich.fill_timezones(rs)
    assert hotel.start_tz == "Europe/Lisbon"
    assert pre.start_tz == "America/Los_Angeles"


def _shanghai_trip() -> list[Reservation]:
    """A flight into China, then the G324 train as the model extracted it from a Trip.com
    confirmation: station names with no IATA codes, and no timezone."""
    flight = ExtractedReservation(
        kind=Kind.FLIGHT,
        title="MU 5401 PVG→CTU",
        start_local="2026-10-13T08:30",
        end_local="2026-10-13T11:50",
        start_iata="PVG",
        end_iata="CTU",
        flight_number="MU5401",
    )
    train = ExtractedReservation(
        kind=Kind.RAIL,
        title="Chengdu East to Beijing West",
        provider="Trip.com",
        confirmation_code="1167733927549689",
        start_local="2026-10-15T15:02",
        end_local="2026-10-15T22:32",
        start_location="Chengdudong(Chengdu East)",
        end_location="Beijingxi(Beijing West)",
        flight_number="G324",
    )
    rs = [enrich.normalise(x, reservation_id=x.title, ingest_id="i") for x in (flight, train)]
    assert all(rs)
    return [r for r in rs if r]


def test_train_without_timezone_departs_where_the_flight_arrived() -> None:
    rs = _shanghai_trip()
    train = rs[1]
    assert (train.start_tz, train.end_tz) == (None, None)
    enrich.fill_timezones(rs)
    assert (train.start_tz, train.end_tz) == ("Asia/Shanghai", "Asia/Shanghai")
    # The feed then says when it leaves in China time, not floating (shown in the viewer's zone).
    ics = calendar([Trip(id="t", owner="me", name="Shanghai", reservations=rs)], "test").decode()
    assert "DTSTART;TZID=Asia/Shanghai:20261015T150200" in ics
    assert "DTEND;TZID=Asia/Shanghai:20261015T223200" in ics


def test_lone_train_takes_its_city_timezone() -> None:
    train = ExtractedReservation(
        kind=Kind.RAIL,
        title="G324",
        start_local="2026-10-15T15:02",
        end_local="2026-10-15T22:32",
        city="Beijing",
    )
    r = enrich.normalise(train, reservation_id="r", ingest_id="i")
    assert r is not None
    enrich.fill_timezones([r])
    assert (r.start_tz, r.end_tz) == ("Asia/Shanghai", "Asia/Shanghai")


def test_cross_border_train_departs_from_previous_arrival() -> None:
    flight = ExtractedReservation(
        kind=Kind.FLIGHT, title="BA 286", start_local="2027-01-20T16:30", end_local="2027-01-21T10:40",
        start_iata="SFO", end_iata="LHR",
    )  # fmt: skip
    # The model's timezone is the city's, which for transport is where it arrives.
    eurostar = ExtractedReservation(
        kind=Kind.RAIL, title="Eurostar 9014", start_local="2027-01-22T08:31", end_local="2027-01-22T11:47",
        city="Paris", timezone="Europe/Paris",
    )  # fmt: skip
    rs = [enrich.normalise(x, reservation_id=x.title, ingest_id="i") for x in (flight, eurostar)]
    assert all(rs)
    rs = [r for r in rs if r]
    enrich.fill_timezones(rs)
    assert (rs[1].start_tz, rs[1].end_tz) == ("Europe/London", "Europe/Paris")
