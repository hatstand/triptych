"""Representative destination photos, from Wikimedia Commons.

A place's photo is the lead image of its English Wikipedia article: editors already chose it to
represent the place, and the API can be told to offer only freely licensed ones. The image itself
lives on Commons, which also has its author and licence. We keep a copy (Wikimedia asks reusers not
to hotlink) and show the credit its licence requires.

Errors are raised, never swallowed: callers run these inside `ctx.run`, so Restate owns retries.
Finding nothing suitable is not an error; it returns None.
"""

import httpx
from bs4 import BeautifulSoup

from triptych.models import PhotoChoice

# Wikimedia's User-Agent policy: name the tool and say how to reach whoever runs it.
USER_AGENT = "Triptych/0.1 (https://github.com/hatstand/triptych) httpx"
WIKIPEDIA_API = "https://en.wikipedia.org/w/api.php"
COMMONS_API = "https://commons.wikimedia.org/w/api.php"

# One of Wikimedia's standard thumbnail widths, which are cached; others may be refused.
THUMB_WIDTH = 960
MIN_WIDTH = 640
"""Smaller originals look poor stretched across the trip page."""
IMAGE_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
"""Bitmaps only: an SVG lead image is a map, flag or coat of arms, not a view of the place."""
MAX_BYTES = 3 * 1024 * 1024
CANDIDATES = 5


class PhotoError(RuntimeError):
    pass


def client(transport: httpx.AsyncBaseTransport | None = None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        headers={"User-Agent": USER_AGENT},
        timeout=20,
        follow_redirects=True,
        transport=transport,
    )


async def _query(http: httpx.AsyncClient, api: str, **params: str | int) -> list[dict]:
    resp = await http.get(api, params={"action": "query", "format": "json", "formatversion": 2, **params})
    resp.raise_for_status()
    return resp.json().get("query", {}).get("pages", [])


def _meta(extmetadata: dict, key: str) -> str | None:
    """A Commons extmetadata field as plain text (Artist is HTML, often a link to a user page)."""
    value = extmetadata.get(key, {}).get("value")
    if not isinstance(value, str):
        return None
    text = BeautifulSoup(value, "html.parser").get_text(" ", strip=True) if "<" in value else value
    return " ".join(text.split())[:200] or None


def _articles(pages: list[dict], place: str) -> list[dict]:
    """Search hits with a free lead image, best first: those titled after the city ("Lisbon" for
    "Lisbon, Portugal"), then in search order. Disambiguation pages never qualify."""
    city = place.split(",")[0].strip().casefold()
    usable = [p for p in pages if p.get("pageimage") and "disambiguation" not in p.get("pageprops", {})]
    return sorted(usable, key=lambda p: (not p["title"].casefold().startswith(city), p.get("index", 0)))


async def choose(place: str, transport: httpx.AsyncBaseTransport | None = None) -> PhotoChoice | None:
    """The photo to show for `place` ("Lisbon, Portugal"), or None if there's nothing suitable."""
    async with client(transport) as http:
        pages = await _query(
            http,
            WIKIPEDIA_API,
            generator="search",
            gsrsearch=place,
            gsrnamespace=0,
            gsrlimit=CANDIDATES,
            prop="pageimages|pageprops|info",
            piprop="name",
            pilicense="free",
            ppprop="disambiguation",
            inprop="url",
        )
        for article in _articles(pages, place):
            files = await _query(
                http,
                COMMONS_API,
                titles=f"File:{article['pageimage']}",
                prop="imageinfo",
                iiprop="url|mime|size|extmetadata",
                iiurlwidth=THUMB_WIDTH,
                iiextmetadatafilter="Artist|LicenseShortName|LicenseUrl",
                iiextmetadatalanguage="en",
            )
            # A file missing from Commons is local to Wikipedia, where only non-free images live.
            if not files or files[0].get("missing") or not files[0].get("imageinfo"):
                continue
            info = files[0]["imageinfo"][0]
            if info.get("mime") not in IMAGE_TYPES or info.get("width", 0) < MIN_WIDTH:
                continue
            meta = info.get("extmetadata", {})
            return PhotoChoice(
                file=files[0]["title"],
                image_url=info.get("thumburl") or info["url"],
                page_url=info["descriptionurl"],
                article_url=article.get("fullurl"),
                artist=_meta(meta, "Artist"),
                licence=_meta(meta, "LicenseShortName"),
                licence_url=_meta(meta, "LicenseUrl"),
            )
    return None


async def download(
    choice: PhotoChoice, transport: httpx.AsyncBaseTransport | None = None
) -> tuple[bytes, str]:
    """The image's bytes and content type."""
    async with client(transport) as http:
        resp = await http.get(choice.image_url)
    resp.raise_for_status()
    content_type = resp.headers.get("content-type", "").split(";")[0].strip()
    if content_type not in IMAGE_TYPES:
        raise PhotoError(f"{choice.image_url} is {content_type or 'untyped'}, not an image we show")
    if len(resp.content) > MAX_BYTES:
        raise PhotoError(f"{choice.image_url} is {len(resp.content)} bytes")
    return resp.content, content_type
