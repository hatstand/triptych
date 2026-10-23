"""Choosing and downloading destination photos, against canned Wikipedia and Commons answers."""

import json
from datetime import datetime
from typing import Any

import httpx
import pytest

from triptych import photos
from triptych.grouping import place_query
from triptych.models import Kind, PhotoChoice, Reservation

pytestmark = pytest.mark.anyio

ARTIST = '<a href="//commons.wikimedia.org/wiki/User:Someone" title="User:Someone">Some  One</a>'


def article(index: int, title: str, image: str | None, *, disambiguation: bool = False) -> dict[str, Any]:
    page: dict[str, Any] = {
        "index": index,
        "title": title,
        "fullurl": f"https://en.wikipedia.org/wiki/{title}",
    }
    if image:
        page["pageimage"] = image
    if disambiguation:
        page["pageprops"] = {"disambiguation": ""}
    return page


def commons_file(name: str, *, mime: str = "image/jpeg", width: int = 4000) -> dict[str, Any]:
    return {
        "title": f"File:{name}",
        "imageinfo": [
            {
                "url": f"https://upload.wikimedia.org/{name}",
                "thumburl": f"https://upload.wikimedia.org/thumb/960px-{name}",
                "descriptionurl": f"https://commons.wikimedia.org/wiki/File:{name}",
                "mime": mime,
                "width": width,
                "extmetadata": {
                    "Artist": {"value": ARTIST},
                    "LicenseShortName": {"value": "CC BY-SA 4.0"},
                    "LicenseUrl": {"value": "https://creativecommons.org/licenses/by-sa/4.0"},
                },
            }
        ],
    }


class Wikimedia:
    """Answers the two API queries `choose` makes, and image downloads; records what was asked."""

    def __init__(self, articles: list[dict[str, Any]], files: dict[str, dict[str, Any]]) -> None:
        self.articles, self.files = articles, files
        self.requests: list[httpx.Request] = []

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "en.wikipedia.org":
            return httpx.Response(200, json={"query": {"pages": self.articles}})
        if request.url.host == "commons.wikimedia.org":
            title = request.url.params["titles"]
            page = self.files.get(title, {"title": title, "missing": True})
            return httpx.Response(200, json={"query": {"pages": [page]}})
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)


async def test_picks_the_article_named_after_the_city_and_credits_its_photo() -> None:
    wiki = Wikimedia(
        [
            article(1, "History of Lisbon", "Old_map.jpg"),
            article(2, "Lisbon", "Lisbon_montage.jpg"),
        ],
        {
            "File:Lisbon_montage.jpg": commons_file("Lisbon_montage.jpg"),
            "File:Old_map.jpg": commons_file("Old_map.jpg"),
        },
    )
    choice = await photos.choose("Lisbon, Portugal", wiki.transport())
    assert choice == PhotoChoice(
        file="File:Lisbon_montage.jpg",
        image_url="https://upload.wikimedia.org/thumb/960px-Lisbon_montage.jpg",
        page_url="https://commons.wikimedia.org/wiki/File:Lisbon_montage.jpg",
        article_url="https://en.wikipedia.org/wiki/Lisbon",
        artist="Some One",
        licence="CC BY-SA 4.0",
        licence_url="https://creativecommons.org/licenses/by-sa/4.0",
    )
    search = wiki.requests[0].url.params
    assert search["gsrsearch"] == "Lisbon, Portugal"
    assert search["pilicense"] == "free"
    assert all(r.headers["user-agent"].startswith("Triptych/") for r in wiki.requests)


async def test_skips_disambiguation_non_commons_vector_and_small_images() -> None:
    wiki = Wikimedia(
        [
            article(1, "Portland", "Portland_logo.png", disambiguation=True),
            article(2, "Portland, Maine", "Local_only.jpg"),
            article(3, "Portland Head Light", "Map.svg"),
            article(4, "Portland Harbor", "Tiny.jpg"),
            article(5, "Old Port", "Old_Port.jpg"),
        ],
        {
            "File:Map.svg": commons_file("Map.svg", mime="image/svg+xml"),
            "File:Tiny.jpg": commons_file("Tiny.jpg", width=300),
            "File:Old_Port.jpg": commons_file("Old_Port.jpg"),
        },
    )
    choice = await photos.choose("Portland, United States", wiki.transport())
    assert choice is not None
    assert choice.file == "File:Old_Port.jpg"


async def test_nothing_suitable_is_none_not_an_error() -> None:
    wiki = Wikimedia([article(1, "Nowhere", None)], {})
    assert await photos.choose("Nowhere", wiki.transport()) is None


async def test_server_errors_raise_so_restate_retries() -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(503))
    with pytest.raises(httpx.HTTPStatusError):
        await photos.choose("Lisbon", transport)


def choice(url: str = "https://upload.wikimedia.org/x.jpg") -> PhotoChoice:
    return PhotoChoice(
        file="File:x.jpg", image_url=url, page_url="https://commons.wikimedia.org/wiki/File:x.jpg"
    )


async def test_download_returns_the_image_and_its_type() -> None:
    wiki = Wikimedia([], {})
    assert await photos.download(choice(), wiki.transport()) == (b"\xff\xd8jpeg", "image/jpeg")


@pytest.mark.parametrize(
    ("body", "content_type"),
    [(b"<html>", "text/html; charset=utf-8"), (b"x" * (photos.MAX_BYTES + 1), "image/jpeg")],
)
async def test_download_refuses_what_we_wont_show(body: bytes, content_type: str) -> None:
    transport = httpx.MockTransport(
        lambda _: httpx.Response(200, content=body, headers={"content-type": content_type})
    )
    with pytest.raises(photos.PhotoError):
        await photos.download(choice(), transport)


def res(
    kind: Kind, city: str, start_location: str | None = None, end_location: str | None = None
) -> Reservation:
    return Reservation(
        id=json.dumps([kind, city]),
        kind=kind,
        title=kind.value,
        start_local=datetime(2026, 11, 12, 9),
        city=city,
        start_location=start_location,
        end_location=end_location,
    )


def test_place_names_the_country_when_an_airport_there_says_which() -> None:
    trip = [
        res(Kind.FLIGHT, "Lisbon", "San Francisco (SFO)", "Lisbon (LIS)"),
        res(Kind.LODGING, "Lisbon"),
        res(Kind.FLIGHT, "San Francisco", "Lisbon (LIS)", "San Francisco (SFO)"),
    ]
    assert place_query(trip) == "Lisbon, Portugal"


def test_place_is_the_destination_alone_without_an_airport_there() -> None:
    assert place_query([res(Kind.LODGING, "Kyoto"), res(Kind.RAIL, "Kyoto", "Tokyo", "Kyoto")]) == "Kyoto"
    assert place_query([]) is None
