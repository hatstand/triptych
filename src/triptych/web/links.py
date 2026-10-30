"""/s/{token}: a trip shared by link, for anyone who has the link, signed in or not (sharing.py)."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from triptych import db, ical, journeys, sharing
from triptych.web import ratelimit
from triptych.web.deps import ics, per_client, signed_in, templates

router = APIRouter(prefix="/s/{token}", dependencies=[per_client(ratelimit.SHARE_VIEW, "share-view")])

LINK_HEADERS = {
    # The token is in the URL: keep it out of Referer headers, search indexes and shared caches.
    "Referrer-Policy": "no-referrer",
    "X-Robots-Tag": "noindex, nofollow",
    "Cache-Control": "private, no-store",
}


async def linked_trip(token: str) -> sharing.LinkedTrip:
    found = await sharing.open_link(token)
    if found is None:
        raise HTTPException(404, "this link doesn't work any more")
    return found


@router.get("", response_class=HTMLResponse)
async def shared_page(request: Request, token: str) -> Response:
    found = await linked_trip(token)
    if me := await signed_in(request):
        access = await db.trip_access(me.id, found.trip_id)
        if access is not None:
            return RedirectResponse(f"/trips/{found.trip_id}", status_code=303)
    t = await db.get_trip(found.owner, found.trip_id)
    if t is None:
        raise HTTPException(404)
    shown = sharing.shared_view(t)
    context = {
        "role": None,
        "trip": shown,
        "trip_url": f"/s/{token}",
        "summary": shown.summary(),
        "days": journeys.timeline(shown.reservations),
    }
    return templates.TemplateResponse(request, "trip.html", context, headers=LINK_HEADERS)


@router.get("/calendar.ics")
async def shared_ics(token: str) -> Response:
    """The shared trip as a calendar to download or subscribe to, as redacted as its page. It goes
    quiet when the link is revoked or expires."""
    found = await linked_trip(token)
    t = await db.get_trip(found.owner, found.trip_id)
    if t is None:
        raise HTTPException(404)
    resp = ics(ical.calendar([sharing.shared_view(t)], t.name), "trip.ics")
    resp.headers.update(LINK_HEADERS)
    return resp


@router.get("/photo")
async def shared_photo(token: str) -> Response:
    found = await linked_trip(token)
    photo = await db.trip_photo(found.owner, found.trip_id)
    if photo is None:
        raise HTTPException(404)
    image, content_type = photo
    # Cached only by this viewer's browser, so a revoked link's photo lingers nowhere else.
    headers = LINK_HEADERS | {"Cache-Control": "private, max-age=86400"}
    return Response(image, media_type=content_type, headers=headers)
