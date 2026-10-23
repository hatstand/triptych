import re
from datetime import datetime, timedelta

from triptych.journeys import Journey, format_duration, timeline
from triptych.models import Kind, Reservation, Trip
from triptych.web import app as web


def leg(
    id: str,
    start: str,
    end: str,
    frm: str,
    to: str,
    tz: tuple[str | None, str | None] = (None, None),
    kind: Kind = Kind.FLIGHT,
    city: str | None = None,
) -> Reservation:
    return Reservation(
        id=id,
        kind=kind,
        title=id,
        start_local=datetime.fromisoformat(start),
        end_local=datetime.fromisoformat(end),
        start_tz=tz[0],
        end_tz=tz[1],
        start_location=frm,
        end_location=to,
        city=city,
    )


def hotel(start: str, end: str) -> Reservation:
    return Reservation(
        id=f"hotel-{start}",
        kind=Kind.LODGING,
        title="Hotel",
        start_local=datetime.fromisoformat(start),
        end_local=datetime.fromisoformat(end),
    )


LHR_SGN = leg(
    "VN50",
    "2026-12-01T11:00",
    "2026-12-02T06:30",
    "London (LHR)",
    "Ho Chi Minh City (SGN)",
    ("Europe/London", "Asia/Ho_Chi_Minh"),
)
SGN_SYD = leg(
    "VN773",
    "2026-12-02T09:45",
    "2026-12-02T21:20",
    "Ho Chi Minh City (SGN)",
    "Sydney (SYD)",
    ("Asia/Ho_Chi_Minh", "Australia/Sydney"),
)


def items(rs: list[Reservation]) -> list[Reservation | Journey]:
    return [i for _, day in timeline(rs) for i in day]


def test_connecting_flights_form_one_journey() -> None:
    [j] = items([SGN_SYD, LHR_SGN])
    assert isinstance(j, Journey)
    assert j.stops == ["LHR", "SGN", "SYD"]
    assert j.layovers == [timedelta(hours=3, minutes=15)]
    assert j.summary == "2 flights"
    # 11:00 GMT -> 21:20 AEDT (UTC+11) is 23h20m elapsed, not the 34h20m of wall-clock times.
    assert j.duration == timedelta(hours=23, minutes=20)
    assert timeline([LHR_SGN, SGN_SYD])[0][0] == LHR_SGN.start_date


def test_overnight_hotel_makes_a_stopover() -> None:
    rs = items([LHR_SGN, hotel("2026-12-02T08:00", "2026-12-03T05:00"), SGN_SYD])
    assert [type(i) for i in rs] == [Reservation, Reservation, Reservation]


def test_long_layover_breaks_the_journey() -> None:
    later = SGN_SYD.model_copy(
        update={"start_local": datetime(2026, 12, 3, 9, 0), "end_local": datetime(2026, 12, 3, 20, 0)}
    )
    assert all(isinstance(i, Reservation) for i in items([LHR_SGN, later]))


def test_departing_elsewhere_is_not_a_connection() -> None:
    elsewhere = SGN_SYD.model_copy(update={"start_location": "Hanoi (HAN)"})
    assert all(isinstance(i, Reservation) for i in items([LHR_SGN, elsewhere]))


def test_trains_connect_by_station_name() -> None:
    a = leg(
        "ES9014",
        "2027-01-21T08:31",
        "2027-01-21T11:47",
        "London St Pancras",
        "Paris Gare du Nord",
        kind=Kind.RAIL,
    )
    b = leg(
        "TGV6201",
        "2027-01-21T13:10",
        "2027-01-21T16:30",
        "paris gare du nord ",
        "Lyon Part-Dieu",
        kind=Kind.RAIL,
    )
    [j] = items([a, b])
    assert isinstance(j, Journey)
    assert j.summary == "2 trains"
    assert j.stops == ["London St Pancras", "Paris Gare du Nord", "Lyon Part-Dieu"]


def test_changing_airport_in_the_same_city_still_connects() -> None:
    a = leg("VN51", "2026-12-20T07:00", "2026-12-20T13:30", "Ho Chi Minh City (SGN)", "London (LHR)")
    b = leg("EZY801", "2026-12-20T18:00", "2026-12-20T19:30", "London (LGW)", "Edinburgh (EDI)")
    [j] = items([a, b])
    assert isinstance(j, Journey)
    assert j.stops == ["SGN", "LHR/LGW", "EDI"]
    assert [c.where for c in j.connections] == ["changing LHR → LGW"]
    assert [c.warning for c in j.connections] == ["Airport change: LHR → LGW, 4h 30m to make it"]
    trip = Trip(id="t", owner="u", name="Edinburgh", reservations=[a, b])
    html = web.templates.get_template("trip.html").render(
        email=None, trip=trip, summary=trip.summary(), days=timeline(trip.reservations)
    )
    html = re.sub(r"\?v=\w+", "", html)  # the sprite's version tag, when the frontend is built
    icon = '<svg class="i" aria-hidden="true"><use href="/static/js/icons.svg#warning"/></svg>'
    assert f'<div class="warning small">{icon} Airport change: LHR → LGW' in html


def test_different_cities_do_not_connect() -> None:
    a = leg("BA1", "2026-12-20T07:00", "2026-12-20T09:00", "London (LHR)", "Boston (BOS)")
    b = leg("UA2", "2026-12-20T12:00", "2026-12-20T15:00", "Newark (EWR)", "Chicago (ORD)")
    assert all(isinstance(i, Reservation) for i in items([a, b]))


def test_iata_city_codes_join_differently_named_airports() -> None:
    # airport data names EWR's city "Newark", but its IATA city code is NYC, like JFK's.
    a = leg("BA1", "2026-12-20T07:00", "2026-12-20T09:00", "London (LHR)", "New York City (JFK)")
    b = leg("UA2", "2026-12-20T13:00", "2026-12-20T15:00", "Newark (EWR)", "Chicago (ORD)")
    [j] = items([a, b])
    assert isinstance(j, Journey)
    assert j.stops == ["LHR", "JFK/EWR", "ORD"]


def test_station_to_airport_in_the_same_city_connects() -> None:
    a = leg(
        "ES9014",
        "2027-01-21T08:31",
        "2027-01-21T11:47",
        "Paris Gare du Nord",
        "London St Pancras",
        kind=Kind.RAIL,
    )
    b = leg("BA117", "2027-01-21T16:00", "2027-01-21T19:00", "London (LHR)", "New York (JFK)")
    [j] = items([a, b])
    assert isinstance(j, Journey)
    assert j.summary == "2 segments"


def test_stations_connect_across_town_using_the_arrival_city() -> None:
    a = leg(
        "ES9014",
        "2027-01-21T08:31",
        "2027-01-21T11:47",
        "London St Pancras",
        "Paris Gare du Nord",
        kind=Kind.RAIL,
        city="Paris",
    )
    b = leg(
        "TGV6201",
        "2027-01-21T13:40",
        "2027-01-21T15:40",
        "Paris Gare de Lyon",
        "Lyon Part-Dieu",
        kind=Kind.RAIL,
    )
    c = leg(
        "TER1", "2027-01-21T13:40", "2027-01-21T15:40", "Lille Flandres", "Lyon Part-Dieu", kind=Kind.RAIL
    )
    assert isinstance(items([a, b])[0], Journey)
    assert all(isinstance(i, Reservation) for i in items([a, c]))


def test_other_items_stay_in_order_around_a_journey() -> None:
    dinner = Reservation(
        id="dinner", kind=Kind.RESTAURANT, title="Dinner", start_local=datetime(2026, 12, 2, 22, 30)
    )
    days = timeline([dinner, SGN_SYD, LHR_SGN])
    assert [(d.day, [type(i).__name__ for i in day]) for d, day in days] == [
        (1, ["Journey"]),
        (2, ["Reservation"]),
    ]


def test_format_duration() -> None:
    assert format_duration(timedelta(minutes=45)) == "45m"
    assert format_duration(timedelta(hours=3, minutes=5)) == "3h 05m"
    assert format_duration(timedelta(days=1, hours=2, minutes=30)) == "1d 2h"


def test_trip_page_renders_journey_with_expandable_segments() -> None:
    trip = Trip(id="t", owner="u", name="Sydney", reservations=[LHR_SGN, SGN_SYD])
    html = web.templates.get_template("trip.html").render(
        email=None, trip=trip, summary=trip.summary(), days=timeline(trip.reservations)
    )
    assert "LHR → SGN → SYD" in html
    assert "3h 15m layover in SGN" in html
    assert "icons.svg#warning" not in html
    assert html.count('<article class="card segment flight">') == 2
    assert "/reservations/VN773/delete" in html
