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
    assert html.count('<article class="card segment flight">') == 2
    assert "/reservations/VN773/delete" in html
