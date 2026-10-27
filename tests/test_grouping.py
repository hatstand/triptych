import itertools
from datetime import date, datetime

from triptych.grouping import assign, pick_destination, transport_stops, trip_title
from triptych.models import Kind, Reservation, TripSummary


def res(kind: Kind, start: str, end: str | None = None, city: str | None = None) -> Reservation:
    return Reservation(
        id=f"{kind}-{start}",
        kind=kind,
        title=kind.value,
        start_local=datetime.fromisoformat(start),
        end_local=datetime.fromisoformat(end) if end else None,
        city=city,
    )


def ids() -> "itertools.count[int]":
    return itertools.count(1)


def test_outbound_hotel_and_return_form_one_trip() -> None:
    counter = ids()
    buckets = assign(
        [
            res(Kind.FLIGHT, "2026-11-12T15:40", "2026-11-13T11:25", "Lisbon"),
            res(Kind.LODGING, "2026-11-13T15:00", "2026-11-17T11:00", "Lisbon"),
            res(Kind.FLIGHT, "2026-11-17T13:05", "2026-11-17T16:50", "San Francisco"),
        ],
        [],
        lambda: f"t{next(counter)}",
    )
    assert len(buckets) == 1
    assert buckets[0].destination == "Lisbon"
    assert (buckets[0].start, buckets[0].end) == (date(2026, 11, 12), date(2026, 11, 17))


def test_new_reservation_joins_existing_trip_with_slack() -> None:
    existing = [
        TripSummary(
            id="lis",
            name="Lisbon",
            destination="Lisbon",
            start_date=date(2026, 11, 12),
            end_date=date(2026, 11, 17),
            reservation_count=3,
        )
    ]
    buckets = assign([res(Kind.RESTAURANT, "2026-11-18T20:30")], existing, lambda: "new")
    assert [b.trip_id for b in buckets] == ["lis"]


def test_far_apart_reservations_make_separate_trips() -> None:
    counter = ids()
    buckets = assign(
        [res(Kind.RAIL, "2027-01-21T08:31", city="Paris"), res(Kind.RESTAURANT, "2026-11-14T20:30")],
        [],
        lambda: f"t{next(counter)}",
    )
    assert len(buckets) == 2
    assert {b.destination for b in buckets} == {"Paris", None}


def test_empty_existing_trip_is_ignored() -> None:
    existing = [
        TripSummary(
            id="x",
            name="x",
            destination=None,
            start_date=date(2026, 11, 12),
            end_date=date(2026, 11, 12),
            reservation_count=0,
        )
    ]
    buckets = assign([res(Kind.ACTIVITY, "2026-11-12T10:00")], existing, lambda: "new")
    assert [b.trip_id for b in buckets] == ["new"]


def test_round_trip_booking_stays_together_even_without_hotel() -> None:
    out = res(Kind.FLIGHT, "2026-11-12T15:40", "2026-11-13T11:25", "Lisbon")
    back = res(Kind.FLIGHT, "2026-11-27T13:05", "2026-11-27T16:50", "San Francisco")
    out.confirmation_code = back.confirmation_code = "QZ7K2M"
    buckets = assign([out, back], [], lambda: "t1")
    assert len(buckets) == 1 and buckets[0].destination == "Lisbon"


def test_bridging_reservation_merges_existing_trips() -> None:
    def trip(id_: str, d0: date, d1: date) -> TripSummary:
        return TripSummary(
            id=id_, name=id_, destination=None, start_date=d0, end_date=d1, reservation_count=1
        )

    existing = [
        trip("out", date(2026, 11, 12), date(2026, 11, 13)),
        trip("back", date(2026, 11, 17), date(2026, 11, 17)),
    ]
    buckets = assign(
        [res(Kind.LODGING, "2026-11-13T15:00", "2026-11-17T11:00", "Lisbon")], existing, lambda: "new"
    )
    assert len(buckets) == 1
    assert buckets[0].trip_id == "out" and buckets[0].absorbed == ["back"]
    assert (buckets[0].start, buckets[0].end) == (date(2026, 11, 12), date(2026, 11, 17))


def test_trip_title_names_where_you_stay_and_when() -> None:
    flight = res(Kind.FLIGHT, "2026-11-12T15:40", "2026-11-13T11:25", "Lisbon")
    lisbon = res(Kind.LODGING, "2026-11-13T15:00", "2026-11-15T11:00", "Lisbon")
    porto = res(Kind.LODGING, "2026-11-15T15:00", "2026-12-02T11:00", "Porto")
    assert trip_title([flight]) == "Lisbon, November 2026"
    assert trip_title([flight, lisbon]) == "Lisbon, November 2026"
    assert trip_title([porto, flight, lisbon]) == "Lisbon & Porto, November–December 2026"


def test_trip_title_caps_cities_and_spans_years() -> None:
    stays = [
        res(Kind.LODGING, f"2026-12-{d}T15:00", None, city)
        for d, city in ((20, "Tokyo"), (22, "Kyoto"), (24, "Osaka"), (26, "Nara"))
    ]
    assert trip_title(stays) == "Tokyo, Kyoto & 2 more, December 2026"
    stays.append(res(Kind.FLIGHT, "2027-01-02T10:00", None, "London"))
    assert trip_title(stays[:3] + stays[4:]) == "Tokyo, Kyoto & Osaka, December 2026–January 2027"
    assert trip_title([res(Kind.ACTIVITY, "2026-12-20T10:00")]) == "Trip, December 2026"


def stay(day: int, city: str, country: str | None) -> Reservation:
    return res(Kind.LODGING, f"2025-08-{day}T15:00", f"2025-08-{day + 1}T11:00", city).model_copy(
        update={"country_code": country}
    )


def test_trip_with_many_stops_is_named_by_country() -> None:
    ferry = res(Kind.FERRY, "2025-08-12T11:30", "2025-08-14T08:30", "Seydisfjordur").model_copy(
        update={"start_location": "Hirtshals", "country_code": "IS"}
    )
    iceland = [
        stay(day, city, "IS")
        for day, city in ((15, "Vik i Myrdal"), (16, "Selfoss"), (17, "Borgarnes"), (18, "Akureyri"))
    ]
    assert trip_title([ferry, *iceland]) == "Iceland, August 2025"
    # A few cities are still named, however many countries they're in.
    assert trip_title(iceland[:3]) == "Vik i Myrdal, Selfoss & Borgarnes, August 2025"
    iberia = [stay(20, "Madrid", "ES"), stay(22, "Seville", "ES")]
    assert trip_title([*iceland[:2], *iberia]) == "Iceland & Spain, August 2025"
    # Too many countries, or a stay in an unknown one, and it lists cities as before.
    many = [*iceland[:1], *iberia, stay(24, "Lisbon", "PT"), stay(26, "Paris", "FR")]
    assert trip_title(many) == "Vik i Myrdal, Madrid & 3 more, August 2025"
    unknown = [*iceland[:3], stay(24, "Somewhere", None)]
    assert trip_title(unknown) == "Vik i Myrdal, Selfoss & 2 more, August 2025"


def leg(start: str, end: str, origin: str, city: str) -> Reservation:
    return res(Kind.FLIGHT, start, end, city).model_copy(update={"start_location": origin})


def test_flights_only_trip_is_named_by_where_it_stays_longest() -> None:
    flights = [
        leg("2026-11-01T21:00", "2026-11-02T17:00", "London (LHR)", "Shanghai"),
        leg("2026-11-04T20:00", "2026-11-05T09:00", "Shanghai (PVG)", "Sydney"),
        leg("2026-11-17T21:00", "2026-11-18T05:00", "Sydney (SYD)", "Shanghai"),
        leg("2026-11-23T11:00", "2026-11-23T16:00", "Shanghai (PVG)", "London"),
    ]
    assert transport_stops(flights) == ["Sydney", "Shanghai"]
    assert pick_destination(flights) == "Sydney"
    assert trip_title(flights) == "Sydney & Shanghai, November 2026"


def test_connections_and_home_are_not_stops() -> None:
    flights = [
        leg("2026-11-01T21:00", "2026-11-02T07:00", "London (LHR)", "Dubai"),
        leg("2026-11-02T10:00", "2026-11-02T22:00", "Dubai (DXB)", "Sydney"),
        leg("2026-11-10T10:00", "2026-11-10T20:00", "Sydney (SYD)", "London"),
        leg("2026-11-12T10:00", "2026-11-12T13:00", "London (LHR)", "Lisbon"),
    ]
    assert transport_stops(flights) == ["Sydney"]
    one_way = [leg("2026-11-01T09:00", "2026-11-01T12:00", "London (LHR)", "Lisbon")]
    assert transport_stops(one_way) == []
    assert trip_title(one_way) == "Lisbon, November 2026"
