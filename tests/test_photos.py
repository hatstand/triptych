"""Choosing and downloading destination photos, against canned Wikipedia and Commons answers."""

import json
from datetime import datetime
from io import BytesIO
from typing import Any

import httpx
import pytest
from PIL import Image

from triptych import photos
from triptych.grouping import place_query
from triptych.models import (
    DEFAULT_FOCUS,
    Kind,
    LlmSubjectRequest,
    Photo,
    PhotoChoice,
    PhotoSubject,
    Reservation,
    SubjectBox,
)

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


def png(width: int, height: int) -> bytes:
    out = BytesIO()
    Image.new("RGB", (width, height)).save(out, "PNG")
    return out.getvalue()


def test_aspect_reads_the_image_and_refuses_what_isnt_one() -> None:
    assert photos.aspect(png(300, 200)) == 1.5
    with pytest.raises(photos.PhotoError):
        photos.aspect(b"<html>")


def size_of(image: bytes) -> tuple[str | None, tuple[int, int]]:
    with Image.open(BytesIO(image)) as im:
        return im.format, im.size


def test_resized_just_covers_the_box_keeping_the_photos_shape() -> None:
    assert size_of(photos.resized(png(960, 640), (144, 144))) == ("WEBP", (216, 144))
    assert size_of(photos.resized(png(640, 960), (432, 288))) == ("WEBP", (432, 648))
    # Never scaled up: the browser can do that as well as we can.
    assert size_of(photos.resized(png(300, 200), (432, 288))) == ("WEBP", (300, 200))
    with pytest.raises(photos.PhotoError):
        photos.resized(b"<html>", (144, 144))


def box(left: float, top: float, right: float, bottom: float) -> SubjectBox:
    return SubjectBox(subject="Tower", left=left, top=top, right=right, bottom=bottom)


def test_subject_box_is_fractions_or_none_when_it_makes_no_sense() -> None:
    assert photos.subject_box(box(10, 0, 40, 95)) == [0.1, 0.0, 0.4, 0.95]
    assert photos.subject_box(box(-5, 10, 120, 50)) == [0.0, 0.1, 1.0, 0.5]  # clamped
    assert photos.subject_box(box(40, 0, 10, 95)) is None  # inside out
    assert photos.subject_box(box(50, 50, 51, 51)) is None  # a detail, not the subject


async def test_locate_subject_asks_the_model_about_the_stored_photo(monkeypatch: pytest.MonkeyPatch) -> None:
    async def place_photo_image(place: str) -> bytes | None:
        return png(400, 300) if place == "Shanghai, China" else None

    async def locate_subject(req: LlmSubjectRequest) -> SubjectBox:
        assert "Shanghai" in req.place and req.image_b64
        return box(30, 2, 45, 100)

    monkeypatch.setattr(photos.db, "place_photo_image", place_photo_image)
    monkeypatch.setattr(photos.llm, "locate_subject", locate_subject)
    subject = await photos.locate_subject("Shanghai, China")
    assert subject == PhotoSubject(aspect=400 / 300, box=[0.3, 0.02, 0.45, 1.0])
    with pytest.raises(photos.PhotoError):
        await photos.locate_subject("Nowhere")


def photo(aspect: float | None, subject: list[float] | None) -> Photo:
    return Photo(file="File:x.jpg", page_url="https://x", version="v", aspect=aspect, subject=subject)


def test_crop_keeps_a_tall_subjects_top_in_a_wide_frame() -> None:
    # A 3:2 skyline whose tower runs from the very top to 80% down, shown in a 5:1 banner: the
    # banner shows 30% of the height, so it starts at the top of the tower.
    assert photo(1.5, [0.3, 0.0, 0.45, 0.8]).position(5) == "50% 0.0%"
    # A short subject in the middle of the lower half is centred: 0.6 - 0.15 = 0.45 of the height
    # is the top of the crop, 0.45 / 0.7 of the way down.
    assert photo(1.5, [0.3, 0.5, 0.5, 0.7]).position(5) == "50% 64.3%"


def test_crop_centres_the_subject_across_when_the_sides_are_cut() -> None:
    # 3:2 in a square: two thirds of the width shows. Subject centred at 0.2: crop from 0 (clamped).
    assert photo(1.5, [0.1, 0.2, 0.3, 0.8]).position(1) == "0.0% 50%"
    assert photo(1.5, [0.8, 0.2, 0.9, 0.8]).position(1) == "100.0% 50%"


def test_crop_without_a_subject_is_the_default() -> None:
    assert photo(1.5, None).position(5) == DEFAULT_FOCUS
    assert photo(None, [0.1, 0.1, 0.2, 0.2]).position(1) == DEFAULT_FOCUS
