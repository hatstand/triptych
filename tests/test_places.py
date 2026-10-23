from triptych import places


def test_lookup_gives_city_country_and_timezone() -> None:
    lhr = places.lookup(" lhr ")
    assert lhr is not None
    assert (lhr.kind, lhr.city, lhr.country_code, lhr.country, lhr.timezone) == (
        "airport",
        "London",
        "GB",
        "United Kingdom",
        "Europe/London",
    )
    ewr = places.lookup("EWR")
    assert ewr is not None and ewr.city == "Newark"
    assert places.lookup("QQS") is not None  # rail stations too (St Pancras)
    assert places.lookup(None) is None and places.lookup("???") is None


def test_same_city_uses_iata_city_codes() -> None:
    assert places.same_city("LHR", "LGW")
    assert places.same_city("JFK", "EWR")
    assert places.same_city("CDG", "ORY")
    assert places.same_city("LHR", "QQS")
    assert not places.same_city("LHR", "CDG")
    assert not places.same_city("LHR", "XXX")
