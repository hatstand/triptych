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
