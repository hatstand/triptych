from urllib.parse import parse_qs, urlsplit

from triptych.web.app import google_calendar_link


def test_google_calendar_link_subscribes_to_the_feed_as_webcal() -> None:
    link = urlsplit(google_calendar_link("https://example.com/feed/abc.ics?u=123"))
    assert (link.netloc, link.path) == ("calendar.google.com", "/calendar/render")
    assert parse_qs(link.query) == {"cid": ["webcal://example.com/feed/abc.ics?u=123"]}
