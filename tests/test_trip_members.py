"""Sharing a trip with other Triptych users: invites, accepting them, and what a viewer sees."""

import os
import uuid
from collections.abc import Iterator
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from triptych import db, sharing
from triptych.auth import Me
from triptych.models import Kind, Reservation, Role, Trip, TripAccess
from triptych.web import app as web
from triptych.web import ratelimit

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def flight(**changes: object) -> Reservation:
    fields: dict[str, object] = dict(
        id=str(uuid.uuid4()), kind=Kind.FLIGHT, title="TP 1351 LIS→OPO", confirmation_code="SECRET1",
        seat="12A", start_local=datetime(2026, 11, 14, 9, 0), end_local=datetime(2026, 11, 14, 10, 0),
        start_location="Lisbon (LIS)", end_location="Porto (OPO)", source_ingest_id="mail-1",
    )  # fmt: skip
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
    return await db.user_for_email("friend@example.com")


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
    web.app.dependency_overrides[web.current_user] = lambda: who[0]
    web.app.dependency_overrides[web.limiter] = ratelimit.limiter
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
    for hidden in ("SECRET1", f"/trips/{trip_id}/rename", "/delete", 'commandfor="share"', "Ask about"):
        assert hidden not in page
    assert "SECRET1" not in client.get(f"/trips/{trip_id}/calendar.ics").text
    assert client.post(f"/trips/{trip_id}/rename", data={"name": "Mine now"}).status_code == 404
    assert "Shared by owner@example.com" in client.get("/").text

    assert client.post(f"/trips/{trip_id}/leave").status_code == 303
    assert client.get(f"/trips/{trip_id}").status_code == 404
