"""`PlacePhoto` virtual object (key = hash of a place): finds and keeps one photo per destination.

    User.file_reservations / remove_reservation ─▶ request(): places with no photo row yet
        ─▶ PlacePhoto.find (one-way) ─▶ choose on Wikipedia/Commons ─▶ download + store
        ─▶ Llm.locate_subject (scope "gpu", its own limit key) ─▶ store the subject's box

The subject is where pages crop the photo around (models.Photo.position). Finding it is the only
GPU work here and the least urgent, so all photos share one queue (lane "photos") rather than
one each, and the photo shows with a default crop until it is done.

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
from triptych.services.llm_service import GPU_SCOPE, limit_key
from triptych.services.llm_service import locate_subject as llm_locate_subject

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
STORE_RETRY = restate.RunOptions[bool](
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
        "places needing photos", db.places_needing_photos, restate.RunOptions[list[str]](), trip_ids
    )
    for place in missing:
        ctx.object_send(find, key(place), PhotoRequest(place=place))


async def search(place: str) -> PhotoSearch:
    # Wrapped: Restate's serde handles a pydantic model, not an Optional of one.
    return PhotoSearch(choice=await photos.choose(place))


async def store(place: str, choice: PhotoChoice | None) -> bool:
    """Download the chosen image and save it; with nothing chosen, or an image we won't show,
    save that there's no photo so the place isn't searched again. Returns whether one was saved."""
    image, content_type, aspect = None, None, None
    if choice is not None:
        try:
            image, content_type = await photos.download(choice)
            aspect = photos.aspect(image)
        except photos.PhotoError as e:
            log.info("no photo for %s: %s", place, e)
            image = None
    await db.save_place_photo(place, choice, image, content_type, aspect)
    return image is not None


@place_photo.handler()
async def find(ctx: restate.ObjectContext, req: PhotoRequest) -> None:
    state = await ctx.run_typed(
        "photo state", db.place_photo_state, restate.RunOptions[db.PhotoState](), req.place
    )
    try:
        if not state.looked_up:
            found = await ctx.run_typed("search", search, SEARCH_RETRY, req.place)
            stored = await ctx.run_typed("download and store", store, STORE_RETRY, req.place, found.choice)
        else:
            stored = state.needs_subject
        if stored:
            subject = await ctx.scope(GPU_SCOPE).service_call(
                llm_locate_subject, req, limit_key=limit_key("photos", "all")
            )
            await ctx.run_typed(
                "store subject",
                db.save_photo_subject,
                restate.RunOptions[None](),
                req.place,
                subject.box,
                subject.aspect,
            )
    except restate.TerminalError as e:
        # Out of retries: what's done stays done, and the worker asks for the rest on its next start.
        log.warning("gave up on a photo for %s: %s", req.place, e)
