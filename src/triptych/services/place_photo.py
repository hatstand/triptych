"""`PlacePhoto` virtual object (key = hash of a place): finds and keeps one photo per destination.

    User.file_reservations / remove_reservation ─▶ request(): places with no photo row yet
        ─▶ PlacePhoto.find (one-way) ─▶ choose on Wikipedia/Commons ─▶ download + store

Keyed by place, so two trips to Lisbon (or two users') share one lookup and one image, and a
second request while the first is running waits its turn and then finds the row already there.
Filing never waits for it: the photo appears on open pages when it is stored (db.save_place_photo
announces the change). The worker sends `find` for older trips on boot (worker.py).
"""

import hashlib
import logging
from datetime import timedelta

import restate

from triptych import db, photos
from triptych.models import PhotoChoice, PhotoRequest, PhotoSearch

log = logging.getLogger("triptych.photos")

place_photo = restate.VirtualObject("PlacePhoto")

# Wikimedia being down or rate-limiting us shouldn't cost a trip its photo, but it's not worth
# retrying for days either: a place with no row is asked for again when the worker next starts.
SEARCH_RETRY = restate.RunOptions[PhotoSearch](
    max_duration=timedelta(hours=1),
    initial_retry_interval=timedelta(seconds=5),
    max_retry_interval=timedelta(minutes=5),
    retry_interval_factor=2.0,
)
STORE_RETRY = restate.RunOptions[None](
    max_duration=timedelta(hours=1),
    initial_retry_interval=timedelta(seconds=5),
    max_retry_interval=timedelta(minutes=5),
    retry_interval_factor=2.0,
)


def key(place: str) -> str:
    # Places have spaces, commas and accents ("São Paulo, Brazil"); keys go into ingress URLs.
    return hashlib.sha256(place.encode()).hexdigest()[:16]


async def request(ctx: restate.ObjectContext, trip_ids: list[str]) -> None:
    """Look up photos for any of these trips' places that haven't been looked up."""
    missing = await ctx.run_typed(
        "places without photos", db.places_without_photos, restate.RunOptions[list[str]](), trip_ids
    )
    for place in missing:
        ctx.object_send(find, key(place), PhotoRequest(place=place))


async def search(place: str) -> PhotoSearch:
    # Wrapped: Restate's serde handles a pydantic model, not an Optional of one.
    return PhotoSearch(choice=await photos.choose(place))


async def store(place: str, choice: PhotoChoice | None) -> None:
    """Download the chosen image and save it; with nothing chosen, or an image we won't show,
    save that there's no photo so the place isn't searched again."""
    image, content_type = None, None
    if choice is not None:
        try:
            image, content_type = await photos.download(choice)
        except photos.PhotoError as e:
            log.info("no photo for %s: %s", place, e)
    await db.save_place_photo(place, choice, image, content_type)


@place_photo.handler()
async def find(ctx: restate.ObjectContext, req: PhotoRequest) -> None:
    if await ctx.run_typed("have photo", db.has_place_photo, restate.RunOptions[bool](), req.place):
        return
    try:
        found = await ctx.run_typed("search", search, SEARCH_RETRY, req.place)
        await ctx.run_typed("download and store", store, STORE_RETRY, req.place, found.choice)
    except restate.TerminalError as e:
        # Out of retries: leave no row, so the worker asks again on its next start.
        log.warning("gave up on a photo for %s: %s", req.place, e)
