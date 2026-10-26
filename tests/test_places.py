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


def test_city_timezone_only_when_unambiguous() -> None:
    assert places.city_timezone(" beijing ") == "Asia/Shanghai"
    assert places.city_timezone("Lisbon") == "Europe/Lisbon"
    assert places.city_timezone("Paris") is None  # Paris, Texas
    assert places.city_timezone("Atlantis") is None and places.city_timezone(None) is None


def test_city_timezone_with_country() -> None:
    assert places.city_timezone("Paris", "FR") == "Europe/Paris"
    assert places.city_timezone("paris", "us") == "America/Chicago"
    assert places.city_timezone("Kashgar", "CN") == "Asia/Urumqi"
    # A city we have no places in takes its country's zone, if one dominates.
    assert places.city_timezone("Leshan", "CN") == "Asia/Shanghai"
    assert places.city_timezone(None, "JP") == "Asia/Tokyo"
    assert places.city_timezone("Smalltown", "US") is None
    assert places.city_timezone("Springfield", "US") is None


def test_an_alias_is_read_as_the_canonical_zone_it_links_to() -> None:
    assert places.canonical_timezone("Europe/Lisbon") == "Europe/Lisbon"
    assert places.canonical_timezone("Asia/Chongqing") == "Asia/Shanghai"
    assert places.canonical_timezone("Europe/Kiev") == "Europe/Kyiv"
    assert places.canonical_timezone("US/Eastern") == "America/New_York"
    for nonsense in ("Pacific/Wellington", "", "../../etc/passwd", "/etc/passwd", "Europe"):
        assert places.canonical_timezone(nonsense) is None, nonsense
