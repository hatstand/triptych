import re
from datetime import datetime, timedelta

from triptych.journeys import Item, Journey, Stay, format_duration, timeline
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


def hotel(start: str, end: str, tz: str | None = None, city: str | None = None) -> Reservation:
    return Reservation(
        id=f"hotel-{start}",
        kind=Kind.LODGING,
        title="Hotel",
        start_local=datetime.fromisoformat(start),
        end_local=datetime.fromisoformat(end),
        start_tz=tz,
        city=city,
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


def items(rs: list[Reservation]) -> list[Item]:
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
    rs = items([LHR_SGN, hotel("2026-12-02T08:00", "2026-12-03T05:00", city="Ho Chi Minh City"), SGN_SYD])
    assert [type(i) for i in rs] == [Reservation, Stay, Reservation, Stay]


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
    html = re.sub(r"/static/assets/icons-[\w-]+\.svg", "/static/icons.svg", html)  # when built
    icon = '<svg class="i" aria-hidden="true"><use href="/static/icons.svg#warning"/></svg>'
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


def test_check_in_waits_for_the_train_that_gets_you_there() -> None:
    # Check-in is allowed from 14:00, but the train doesn't arrive until 18:00.
    train = leg(
        "ES9024",
        "2027-03-05T15:01",
        "2027-03-05T18:20",
        "London St Pancras",
        "Paris Gare du Nord",
        ("Europe/London", "Europe/Paris"),
        kind=Kind.RAIL,
        city="Paris",
    )
    stay = hotel("2027-03-05T14:00", "2027-03-08T11:00", "Europe/Paris", "Paris")
    [(_, first), (_, last)] = timeline([stay, train])
    assert first == [train, Stay(stay)]
    assert last == [Stay(stay, checkout=True)]
    trip = Trip(id="t", owner="u", name="Paris", reservations=[stay, train])
    html = web.templates.get_template("trip.html").render(
        email=None, trip=trip, summary=trip.summary(), days=timeline(trip.reservations)
    )
    assert html.index("ES9024") < html.index("Check-in from 14:00") < html.index("Check out")
    assert "3 nights" in html
    # The clock only goes forward: the hotel's 14:00 and 11:00 are on its cards, not in the clock.
    clocks = re.findall(r'<time datetime="[^"]+">(\d\d:\d\d)</time>', html)
    assert clocks == ["15:01", "18:20"]
    assert "By 11:00" in html


def test_check_out_comes_before_an_early_departure() -> None:
    stay = hotel("2027-03-05T14:00", "2027-03-08T11:00", "Europe/Paris", "Paris")
    train = leg(
        "TGV6601",
        "2027-03-08T07:30",
        "2027-03-08T09:30",
        "Paris Gare de Lyon",
        "Lyon Part-Dieu",
        ("Europe/Paris", "Europe/Paris"),
        kind=Kind.RAIL,
        city="Lyon",
    )
    assert timeline([stay, train])[-1][1] == [Stay(stay, checkout=True), train]


def test_only_arrivals_in_the_hotels_city_move_its_check_in() -> None:
    # Arriving in Lyon at 12:00 and going on to Marseille at 20:00: the Lyon hotel isn't after that.
    into = leg("A", "2027-03-05T10:00", "2027-03-05T12:00", "Paris", "Lyon", kind=Kind.RAIL, city="Lyon")
    onward = leg(
        "B", "2027-03-05T20:00", "2027-03-05T22:00", "Lyon", "Marseille", kind=Kind.RAIL, city="Marseille"
    )
    stay = hotel("2027-03-05T15:00", "2027-03-05T19:00", city="Lyon")
    assert items([onward, stay, into]) == [into, Stay(stay), onward]


def test_times_in_different_zones_are_compared_as_instants() -> None:
    # 17:00 in New York is after 18:00 in London on the same day.
    flight = leg(
        "BA117",
        "2027-03-05T18:00",
        "2027-03-05T21:00",
        "London (LHR)",
        "New York (JFK)",
        ("Europe/London", "America/New_York"),
    )
    early = Reservation(
        id="dinner",
        kind=Kind.RESTAURANT,
        title="Dinner",
        start_local=datetime(2027, 3, 5, 17, 0),
        start_tz="America/New_York",
    )
    assert items([early, flight]) == [flight, early]


def test_a_hotel_at_the_destination_does_not_break_the_journey() -> None:
    # Check-in in Lisbon opens at 15:00, during the London layover; it's not a stopover in London.
    a = leg(
        "BA286", "2026-11-05T19:40", "2026-11-06T13:55", "San Francisco (SFO)", "London (LHR)", city="London"
    )
    b = leg("BA500", "2026-11-06T16:10", "2026-11-06T18:50", "London (LHR)", "Lisbon (LIS)", city="Lisbon")
    stay = hotel("2026-11-06T15:00", "2026-11-09T11:00", city="Lisbon")
    assert items([a, b, stay])[:2] == [Journey([a, b]), Stay(stay)]


def test_ferries_connect_and_arrive_on_the_timeline() -> None:
    a = leg("Calais", "2027-06-01T09:00", "2027-06-01T10:30", "Dover", "Calais", kind=Kind.FERRY)
    b = leg("Bastia", "2027-06-01T20:00", "2027-06-02T08:00", "Calais", "Bastia", kind=Kind.FERRY)
    [j] = items([a, b])
    assert isinstance(j, Journey)
    assert j.summary == "2 ferries"
    single = leg(
        "Hydra",
        "2027-06-05T09:00",
        "2027-06-05T11:00",
        "Piraeus",
        "Hydra",
        ("Europe/Athens", "Europe/Athens"),
        Kind.FERRY,
    )
    trip = Trip(id="t", owner="u", name="Hydra", reservations=[single])
    html = web.templates.get_template("trip.html").render(
        email=None, trip=trip, summary=trip.summary(), days=timeline(trip.reservations)
    )
    assert '<li class="entry ferry">' in html
    assert re.search(r'icons(-[\w-]+)?\.svg#ferry"', html)
    assert '<div class="stop"></div>' in html  # it arrives, like a flight or train
    assert "2h 00m" in html
