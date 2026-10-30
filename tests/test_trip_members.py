"""Sharing a trip with other Triptych users: invites, accepting them, and what a viewer sees."""

import os
import uuid
from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from triptych import db, senders, sharing
from triptych.auth import Me
from triptych.models import AddReservation, Answer, AskTrip, Kind, Reservation, Role, Trip, TripAccess
from triptych.services import assistant
from triptych.web import app as web
from triptych.web import deps, ratelimit

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def flight(**changes: object) -> Reservation:
    fields: dict[str, object] = {
        "id": str(uuid.uuid4()), "kind": Kind.FLIGHT, "title": "TP 1351 LIS→OPO",
        "confirmation_code": "SECRET1", "seat": "12A",
        "start_local": datetime(2026, 11, 14, 9, 0), "end_local": datetime(2026, 11, 14, 10, 0),
        "start_location": "Lisbon (LIS)", "end_location": "Porto (OPO)", "source_ingest_id": "mail-1",
    }  # fmt: skip
    return Reservation.model_validate(fields | changes)


def test_a_viewer_sees_everything_but_the_codes() -> None:
    trip = Trip(id="t1", owner="owner-1", name="Porto", reservations=[flight()])
    (seen,) = sharing.viewer_view(trip).reservations
    assert (seen.confirmation_code, seen.source_ingest_id) == (None, None)
    assert (seen.seat, seen.title) == ("12A", "TP 1351 LIS→OPO")


# --- with a database -------------------------------------------------------------------------


@pytest.fixture
async def owner(database: str) -> str:
    return await db.user_for_email("owner@example.com")


@pytest.fixture
async def friend(database: str) -> str:
    friend = await db.user_for_email("friend@example.com")
    await senders.mark_verified("friend@example.com")
    return friend


async def a_trip(owner: str, **changes: object) -> str:
    filed = await db.file_reservations(owner, f"mail-{uuid.uuid4()}", [flight(**changes)])
    (trip_id,) = filed.trip_ids
    return trip_id


@needs_db
@pytest.mark.anyio
async def test_an_invite_gives_access_only_once_accepted(owner: str, friend: str) -> None:
    trip_id = await a_trip(owner)
    await sharing.invite(owner, trip_id, " Friend@Example.com ")
    assert await sharing.members(owner, trip_id) == [
        sharing.Member(email="friend@example.com", role=Role.VIEW, accepted=False)
    ]
    (invite,) = await sharing.pending_invites(friend)
    assert (invite.trip_id, invite.role, invite.invited_by) == (trip_id, Role.VIEW, "owner@example.com")
    assert await db.trip_access(friend, trip_id) is None
    assert await db.shared_trip_summaries(friend) == []

    assert await sharing.accept(friend, trip_id)
    assert await sharing.pending_invites(friend) == []
    assert await db.trip_access(friend, trip_id) == TripAccess(trip_id=trip_id, owner=owner, role=Role.VIEW)
    (shared,) = await db.shared_trip_summaries(friend)
    assert (shared.id, shared.role, shared.shared_by) == (trip_id, Role.VIEW, "owner@example.com")
    assert (await sharing.members(owner, trip_id))[0].accepted
    assert not await sharing.accept(friend, trip_id)  # nothing left to accept

    await sharing.leave(friend, trip_id)
    assert await db.trip_access(friend, trip_id) is None
    assert await sharing.members(owner, trip_id) == []


@needs_db
@pytest.mark.anyio
async def test_an_invite_waits_for_its_address_to_join_an_account(owner: str, friend: str) -> None:
    trip_id = await a_trip(owner)
    await sharing.invite(owner, trip_id, "later@example.com")
    assert await sharing.pending_invites(friend) == []
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO user_email (email, user_id) VALUES ('later@example.com', %s)", (friend,)
        )
    assert await sharing.pending_invites(friend) == []  # not until it's proven to be theirs
    assert not await sharing.accept(friend, trip_id)
    await senders.mark_verified("Later@example.com")
    (invite,) = await sharing.pending_invites(friend)
    assert invite.trip_id == trip_id
    assert await sharing.accept(friend, trip_id)
    assert await db.trip_access(friend, trip_id) is not None


@needs_db
@pytest.mark.anyio
async def test_only_the_owner_invites_and_never_themselves(owner: str, friend: str) -> None:
    trip_id = await a_trip(owner)
    await sharing.invite(owner, trip_id, "owner@example.com")
    await sharing.invite(owner, trip_id, "not an address")
    await sharing.invite(friend, trip_id, "friend@example.com")
    assert await sharing.members(owner, trip_id) == []
    assert not await sharing.accept(owner, trip_id)


@needs_db
@pytest.mark.anyio
async def test_declining_or_removing_ends_it(owner: str, friend: str) -> None:
    trip_id = await a_trip(owner)
    await sharing.invite(owner, trip_id, "friend@example.com")
    await sharing.decline(friend, trip_id)
    assert await sharing.members(owner, trip_id) == []

    await sharing.invite(owner, trip_id, "friend@example.com")
    assert await sharing.accept(friend, trip_id)
    await sharing.remove_member(owner, trip_id, "FRIEND@example.com")
    assert await db.trip_access(friend, trip_id) is None


@needs_db
@pytest.mark.anyio
async def test_merging_keeps_everyone_with_the_more_generous_role(owner: str, friend: str) -> None:
    lisbon = await a_trip(owner)
    porto = await a_trip(owner, start_local=datetime(2027, 3, 1, 9, 0), end_local=datetime(2027, 3, 1, 10, 0))
    await sharing.invite(owner, lisbon, "friend@example.com", Role.EDIT)
    await sharing.invite(owner, porto, "friend@example.com", Role.VIEW)
    await sharing.invite(owner, lisbon, "other@example.com")
    await db.merge_trips(owner, lisbon, porto)
    members = {m.email: m.role for m in await sharing.members(owner, porto)}
    assert members == {"friend@example.com": Role.EDIT, "other@example.com": Role.VIEW}


@pytest.fixture
def as_user() -> Iterator[list[Me]]:
    who: list[Me] = []
    web.app.dependency_overrides[deps.current_user] = lambda: who[0]
    web.app.dependency_overrides[deps.limiter] = ratelimit.limiter
    yield who
    web.app.dependency_overrides.clear()


@needs_db
@pytest.mark.anyio
async def test_a_viewer_gets_a_read_only_trip_page_without_codes(
    owner: str, friend: str, as_user: list[Me]
) -> None:
    trip_id = await a_trip(owner)
    client = TestClient(web.app, follow_redirects=False)
    as_user.append(Me(id=owner, email="owner@example.com"))
    assert client.post(f"/trips/{trip_id}/members", data={"email": "friend@example.com"}).status_code == 303
    assert "friend@example.com" in client.get(f"/trips/{trip_id}").text

    as_user[0] = Me(id=friend, email="friend@example.com")
    assert client.get(f"/trips/{trip_id}").status_code == 404
    home = client.get("/").text
    assert "invited you to see this trip" in home and f"/trips/{trip_id}/accept" in home
    assert client.post(f"/trips/{trip_id}/accept").status_code == 303

    page = client.get(f"/trips/{trip_id}").text
    assert "TP 1351 LIS→OPO" in page and "12A" in page and "Shared by owner@example.com" in page
    for hidden in ("SECRET1", f"/trips/{trip_id}/rename", "/delete", 'commandfor="share"'):
        assert hidden not in page
    assert "SECRET1" not in client.get(f"/trips/{trip_id}/calendar.ics").text
    assert client.post(f"/trips/{trip_id}/rename", data={"name": "Mine now"}).status_code == 404
    assert "Shared by owner@example.com" in client.get("/").text

    assert client.post(f"/trips/{trip_id}/leave").status_code == 303
    assert client.get(f"/trips/{trip_id}").status_code == 404


@needs_db
@pytest.mark.anyio
async def test_an_editor_can_change_the_trip_but_not_share_or_delete_it(
    owner: str, friend: str, as_user: list[Me]
) -> None:
    trip_id = await a_trip(owner)
    await sharing.invite(owner, trip_id, "friend@example.com", Role.EDIT)
    assert await sharing.accept(friend, trip_id)
    assert (
        await db.trip_access(friend, trip_id) or TripAccess(trip_id="", owner="", role=Role.VIEW)
    ).role is Role.EDIT

    calls: list[tuple[str, object]] = []

    class Restate:
        async def object_call(self, _handler: object, key: str, req: object) -> None:
            calls.append((key, req))

    web.app.dependency_overrides[deps.rs] = Restate
    as_user.append(Me(id=friend, email="friend@example.com"))
    client = TestClient(web.app, follow_redirects=False)
    page = client.get(f"/trips/{trip_id}").text
    assert "SECRET1" in page and f"/trips/{trip_id}/rename" in page and 'commandfor="add-reservation"' in page
    for owners_only in (
        'commandfor="share"',
        f"/trips/{trip_id}/delete",
        f"/trips/{trip_id}/merge",
    ):
        assert owners_only not in page

    form = {
        "kind": "rail",
        "title": "Porto → Braga",
        "start_local": "2026-11-15T10:00",
        "ingest_id": "mail-1",
    }
    assert client.post(f"/trips/{trip_id}/reservations", data=form).status_code == 303
    assert client.post(f"/trips/{trip_id}/rename", data={"name": "Porto with Alex"}).status_code == 303
    (key, added), (key2, _) = calls
    assert key == key2 == owner  # serialised with the owner's own changes
    assert isinstance(added, AddReservation)
    assert (added.added_by, added.ingest_id) == (friend, None)  # the owner's emails aren't theirs to name
    assert client.post(f"/trips/{trip_id}/delete").status_code == 404
    assert client.post(f"/trips/{trip_id}/links").status_code == 404

    # What they add says so, to everyone on the trip.
    await db.add_reservation(owner, trip_id, added.reservation_id, None, added.fields, added.added_by)
    assert "Added by friend@example.com" in client.get(f"/trips/{trip_id}").text


@needs_db
@pytest.mark.anyio
async def test_the_owner_can_switch_someone_between_viewer_and_editor(
    owner: str, friend: str, as_user: list[Me]
) -> None:
    trip_id = await a_trip(owner)
    as_user.append(Me(id=owner, email="owner@example.com"))
    client = TestClient(web.app, follow_redirects=False)
    invite = {"email": "friend@example.com", "role": "edit"}
    assert client.post(f"/trips/{trip_id}/members", data=invite).status_code == 303
    assert await sharing.accept(friend, trip_id)
    assert "Make viewer" in client.get(f"/trips/{trip_id}").text
    assert client.post(f"/trips/{trip_id}/members", data=invite | {"role": "view"}).status_code == 303
    assert await sharing.members(owner, trip_id) == [
        sharing.Member(email="friend@example.com", role=Role.VIEW, accepted=True)
    ]
    assert client.post(f"/trips/{trip_id}/members", data=invite | {"role": "owner"}).status_code == 422


@needs_db
@pytest.mark.anyio
async def test_shared_trips_are_in_the_members_calendar_feed_until_hidden(
    owner: str, friend: str, as_user: list[Me]
) -> None:
    trip_id = await a_trip(owner)
    await sharing.invite(owner, trip_id, "friend@example.com")
    client = TestClient(web.app, follow_redirects=False)
    feed = f"/feed/{web.feed_token(friend)}.ics?u={friend}"
    assert "TP 1351" not in client.get(feed).text  # not until accepted

    assert await sharing.accept(friend, trip_id)
    ics = client.get(feed).text
    assert "TP 1351" in ics and "SECRET1" not in ics  # a viewer's: no codes

    as_user.append(Me(id=friend, email="friend@example.com"))
    assert "Hide from my calendar" in client.get(f"/trips/{trip_id}").text
    assert client.post(f"/trips/{trip_id}/feed", data={"shown": "false"}).status_code == 303
    assert "TP 1351" not in client.get(feed).text
    assert "Show in my calendar" in client.get(f"/trips/{trip_id}").text

    await sharing.set_in_feed(friend, trip_id, True)
    await sharing.invite(owner, trip_id, "friend@example.com", Role.EDIT)
    assert "SECRET1" in client.get(feed).text  # an editor's has them


@needs_db
@pytest.mark.anyio
async def test_members_can_ask_but_a_viewers_answer_never_sees_the_codes(
    owner: str, friend: str, as_user: list[Me]
) -> None:
    trip_id = await a_trip(owner)
    await sharing.invite(owner, trip_id, "friend@example.com")
    assert await sharing.accept(friend, trip_id)
    asked: list[AskTrip] = []

    class Restate:
        async def service_call(self, _handler: object, req: AskTrip) -> Answer:
            asked.append(req)
            return Answer(text="Leave at 7.")

    web.app.dependency_overrides[deps.rs] = Restate
    as_user.append(Me(id=friend, email="friend@example.com"))
    client = TestClient(web.app, follow_redirects=False)
    assert "Ask about this trip" in client.get(f"/trips/{trip_id}").text
    resp = client.post(f"/trips/{trip_id}/ask", data={"question": "When do I leave?"})
    assert resp.status_code == 200 and "Leave at 7." in resp.text
    (req,) = asked
    assert (req.owner, req.asker, req.viewer) == (owner, friend, True)

    assert "SECRET1" in await assistant._itinerary(owner, trip_id, viewer=False)
    assert "SECRET1" not in await assistant._itinerary(owner, trip_id, viewer=True)
