"""/trips/{trip_id}: a trip's page and everything done to it, by its owner and the people it's
shared with (sharing.py)."""

import asyncio
import uuid
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from pydantic import field_validator, model_validator

from triptych import auth, db, enrich, ical, journeys, photos, sharing
from triptych.config import settings
from triptych.models import (
    AddReservation,
    Answer,
    AskTrip,
    EditReservation,
    MergeTrips,
    RemoveReservation,
    RenameTrip,
    ReservationFields,
    Role,
    Trip,
    TripAccess,
    TripRef,
)
from triptych.services import assistant as assistant_svc
from triptych.services import user as user_svc
from triptych.web import ratelimit
from triptych.web.deps import CanEdit, CanView, Client, Owns, User, ics, load_trip, per_user, templates

router = APIRouter(prefix="/trips/{trip_id}")


async def trip_context(me: auth.Me, access: TripAccess, t: Trip) -> dict[str, Any]:
    """What trip.html needs to show a trip to someone with `access`."""
    owns = access.role is Role.OWNER
    # Other trips to merge into, where bookings came from and how it's shared are the owner's alone.
    others = [s for s in await db.trip_summaries(me.id) if s.id != t.id] if owns else []
    sources = await db.trip_sources(access.owner, t.id) if owns else []
    links = await sharing.links(access.owner, t.id) if owns else []
    members = await sharing.members(access.owner, t.id) if owns else []
    editors = {r.added_by for r in t.reservations if r.added_by}
    return {
        "email": me.email,
        "role": access.role,
        "trip": t,
        "trip_url": f"/trips/{t.id}",
        "summary": t.summary(),
        "days": journeys.timeline(t.reservations),
        "others": others,
        "sources": sources,
        "links": links,
        "members": members,
        "added_by": {e: await db.primary_email(e) or "someone" for e in editors},
        "shared_by": None if owns else await db.primary_email(access.owner),
        "in_feed": None if owns else await sharing.in_feed(me.id, t.id),
        "base_url": settings.public_base_url.rstrip("/"),
    }


@router.get("", response_class=HTMLResponse)
async def trip_page(request: Request, me: User, access: CanView) -> Response:
    t = await load_trip(access)
    return templates.TemplateResponse(request, "trip.html", await trip_context(me, access, t))


@router.get("/photo")
async def trip_photo(trip_id: str, access: CanView, size: Literal["thumb", "card"] | None = None) -> Response:
    """The trip's destination photo, or with `size` a smaller copy for a trip card (photos.SIZES).
    Pages link it with the photo's version in the query string, so a new photo is a new URL and this
    one can be cached for good."""
    photo = await db.trip_photo(access.owner, trip_id)
    if photo is None:
        raise HTTPException(404)
    image, content_type = photo
    if size:
        image, content_type = await asyncio.to_thread(photos.resized, image, photos.SIZES[size]), "image/webp"
    return Response(
        image, media_type=content_type, headers={"Cache-Control": "private, max-age=31536000, immutable"}
    )


@router.post("/ask", response_class=HTMLResponse, dependencies=[per_user(ratelimit.ASK, "ask")])
async def trip_ask(
    request: Request,
    trip_id: str,
    me: User,
    access: CanView,
    client: Client,
    question: Annotated[str, Form()],
) -> Response:
    t = await load_trip(access)
    req = AskTrip(
        owner=access.owner,
        trip_id=trip_id,
        question=question,
        asker=None if access.role is Role.OWNER else me.id,
        viewer=access.role is Role.VIEW,
    )
    answer: Answer = await client.service_call(assistant_svc.ask, req)
    context = await trip_context(me, access, t)
    return templates.TemplateResponse(
        request, "trip.html", context | {"question": question, "answer": answer.text}
    )


@router.post("/links", dependencies=[per_user(ratelimit.SHARE_MANAGE, "share")])
async def link_create(
    access: Owns, label: Annotated[str, Form()] = "", expire: Annotated[bool, Form()] = False
) -> Response:
    await sharing.create_link(access.owner, access.trip_id, label[:100], expire_after_trip=expire)
    return RedirectResponse(f"/trips/{access.trip_id}#share", status_code=303)


@router.post("/links/{link_id}/delete")
async def link_revoke(link_id: str, access: Owns) -> Response:
    await sharing.revoke_link(access.owner, access.trip_id, link_id)
    return RedirectResponse(f"/trips/{access.trip_id}#share", status_code=303)


@router.post("/members", dependencies=[per_user(ratelimit.SHARE_MANAGE, "share")])
async def member_invite(
    access: Owns, email: Annotated[str, Form()], role: Annotated[Literal["view", "edit"], Form()] = "view"
) -> Response:
    await sharing.invite(access.owner, access.trip_id, email[:320], Role(role))
    return RedirectResponse(f"/trips/{access.trip_id}#share", status_code=303)


@router.post("/members/remove")
async def member_remove(access: Owns, email: Annotated[str, Form()]) -> Response:
    await sharing.remove_member(access.owner, access.trip_id, email)
    return RedirectResponse(f"/trips/{access.trip_id}#share", status_code=303)


@router.post("/leave")
async def member_leave(me: User, access: CanView) -> Response:
    await sharing.leave(me.id, access.trip_id)
    return RedirectResponse("/", status_code=303)


@router.post("/feed")
async def member_feed(me: User, access: CanView, shown: Annotated[bool, Form()]) -> Response:
    """Put a trip shared with you in your calendar feed, or take it out."""
    await sharing.set_in_feed(me.id, access.trip_id, shown)
    return RedirectResponse(f"/trips/{access.trip_id}", status_code=303)


# Invitees have no access to the trip until they accept, so these take no trip_access.
@router.post("/accept")
async def invite_accept(trip_id: str, me: User) -> Response:
    if not await sharing.accept(me.id, trip_id):
        raise HTTPException(404, "invite not found")
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@router.post("/decline")
async def invite_decline(trip_id: str, me: User) -> Response:
    await sharing.decline(me.id, trip_id)
    return RedirectResponse("/", status_code=303)


@router.post("/rename")
async def trip_rename(
    trip_id: str, access: CanEdit, client: Client, name: Annotated[str, Form()]
) -> Response:
    await client.object_call(user_svc.rename_trip, access.owner, RenameTrip(trip_id=trip_id, name=name))
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@router.post("/merge")
async def trip_merge(
    trip_id: str, me: User, access: Owns, client: Client, into: Annotated[str, Form()]
) -> Response:
    target = await db.trip_access(me.id, into)
    if target is None or target.role is not Role.OWNER:
        raise HTTPException(404, "trip not found")
    await client.object_call(user_svc.merge_trips, access.owner, MergeTrips(trip_id=trip_id, into=into))
    return RedirectResponse(f"/trips/{into}", status_code=303)


class ReservationForm(ReservationFields):
    """The trip page's edit form (trip.html): a blank field is none, travelers are one per line."""

    @field_validator("*", mode="before")
    @classmethod
    def blank_is_none(cls, v: Any) -> Any:
        if isinstance(v, str):
            return v.strip() or None
        return v

    @field_validator("travelers", mode="before")
    @classmethod
    def one_per_line(cls, v: Any) -> Any:
        lines = v if isinstance(v, list) else [v or ""]
        return [name.strip() for line in lines for name in str(line).splitlines() if name.strip()]

    @field_validator("start_tz", "end_tz")
    @classmethod
    def known_zone(cls, v: str | None) -> str | None:
        if v is not None and enrich.valid_tz(v) is None:
            raise ValueError(f"{v} isn't a timezone we know, like Europe/Lisbon")
        return v

    @model_validator(mode="after")
    def ends_after_it_starts(self) -> ReservationForm:
        """The timeline runs forward in time, so nothing may end before it starts. Wall clocks in two
        zones are compared as instants; without both zones, as the times written."""
        if self.end_local is None:
            return self
        start, end = self.start_local, self.end_local
        if self.start_tz and self.end_tz:
            start, end = enrich.to_utc(start, self.start_tz), enrich.to_utc(end, self.end_tz)
        elif self.start_tz or self.end_tz:
            return self  # one zone unknown: it could be either way round
        if start is not None and end is not None and end < start:
            raise ValueError("it ends before it starts")
        return self


class AddReservationForm(ReservationForm):
    ingest_id: str | None = None
    """Which of the trip's emails it was in ("" for none)."""


@router.post("/reservations")
async def reservation_add(
    trip_id: str, me: User, access: CanEdit, client: Client, form: Annotated[AddReservationForm, Form()]
) -> Response:
    fields = ReservationFields.model_validate(form.model_dump(exclude={"ingest_id"}))
    owns = access.role is Role.OWNER
    req = AddReservation(
        trip_id=trip_id,
        reservation_id=str(uuid.uuid4()),
        # Which email missed it is the owner's to say: the emails are theirs.
        ingest_id=form.ingest_id if owns else None,
        fields=fields,
        added_by=None if owns else me.id,
    )
    await client.object_call(user_svc.add_reservation, access.owner, req)
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@router.post("/reservations/{reservation_id}/edit")
async def reservation_edit(
    trip_id: str,
    reservation_id: str,
    access: CanEdit,
    client: Client,
    form: Annotated[ReservationForm, Form()],
) -> Response:
    fields = ReservationFields.model_validate(form.model_dump())
    await client.object_call(
        user_svc.edit_reservation,
        access.owner,
        EditReservation(trip_id=trip_id, reservation_id=reservation_id, fields=fields),
    )
    return RedirectResponse(f"/trips/{trip_id}", status_code=303)


@router.post("/delete")
async def trip_delete(trip_id: str, access: Owns, client: Client) -> Response:
    await client.object_call(user_svc.delete_trip, access.owner, TripRef(trip_id=trip_id))
    return RedirectResponse("/", status_code=303)


@router.post("/reservations/{reservation_id}/delete")
async def reservation_delete(trip_id: str, reservation_id: str, access: CanEdit, client: Client) -> Response:
    still_exists = await client.object_call(
        user_svc.remove_reservation,
        access.owner,
        RemoveReservation(trip_id=trip_id, reservation_id=reservation_id),
    )
    return RedirectResponse(f"/trips/{trip_id}" if still_exists else "/", status_code=303)


@router.get("/calendar.ics")
async def trip_ics(trip_id: str, access: CanView) -> Response:
    t = await load_trip(access)
    return ics(ical.calendar([t], t.name), f"{trip_id}.ics")
