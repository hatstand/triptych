"""Who may do what with a trip: every /trips/{trip_id} route checks the caller's access first."""

import os
import uuid
from collections.abc import Iterator
from datetime import datetime
from typing import Any

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from triptych import db
from triptych.auth import Me
from triptych.models import Kind, Reservation, Role, TripAccess
from triptych.web import app as web

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")

# The least each trip route needs. A new trip route fails test_every_trip_route_says_who_may_use_it
# until it's listed here.
NEEDS: dict[tuple[str, str], Role | None] = {
    ("GET", "/trips/{trip_id}"): Role.VIEW,
    ("GET", "/trips/{trip_id}/photo"): Role.VIEW,
    ("GET", "/trips/{trip_id}/calendar.ics"): Role.VIEW,
    ("GET", "/api/trips/{trip_id}"): Role.VIEW,
    ("POST", "/trips/{trip_id}/rename"): Role.EDIT,
    ("POST", "/trips/{trip_id}/reservations"): Role.EDIT,
    ("POST", "/trips/{trip_id}/reservations/{reservation_id}/edit"): Role.EDIT,
    ("POST", "/trips/{trip_id}/reservations/{reservation_id}/delete"): Role.EDIT,
    ("POST", "/trips/{trip_id}/ask"): Role.OWNER,
    ("POST", "/trips/{trip_id}/merge"): Role.OWNER,
    ("POST", "/trips/{trip_id}/delete"): Role.OWNER,
    ("POST", "/trips/{trip_id}/links"): Role.OWNER,
    ("POST", "/trips/{trip_id}/links/{link_id}/delete"): Role.OWNER,
    ("POST", "/trips/{trip_id}/members"): Role.OWNER,
    ("POST", "/trips/{trip_id}/members/remove"): Role.OWNER,
    ("POST", "/trips/{trip_id}/leave"): Role.VIEW,
    ("POST", "/trips/{trip_id}/feed"): Role.VIEW,
    # Invitees have no access until they accept.
    ("POST", "/trips/{trip_id}/accept"): None,
    ("POST", "/trips/{trip_id}/decline"): None,
}


def needed(route: APIRoute) -> Role | None:
    """The role a route's trip_access dependency asks for, if it has one."""
    for dep in route.dependant.dependencies:
        call: Any = dep.call
        if call is not None and call.__qualname__ == "trip_access.<locals>.check":
            return call.__closure__[0].cell_contents
    return None


def test_every_trip_route_says_who_may_use_it() -> None:
    found = {
        (method, route.path): needed(route)
        for route in web.app.routes
        if isinstance(route, APIRoute) and "{trip_id}" in route.path
        for method in route.methods or ()
    }
    assert found == NEEDS


def test_each_role_allows_the_ones_below_it() -> None:
    assert Role.OWNER.allows(Role.EDIT) and Role.OWNER.allows(Role.VIEW)
    assert Role.EDIT.allows(Role.VIEW) and not Role.EDIT.allows(Role.OWNER)
    assert not Role.VIEW.allows(Role.EDIT)


ME = Me(id="user-1", email="me@example.com")


@pytest.fixture
def as_role(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Role | None]]:
    """Set role[0] to what the caller may do with every trip (None: nothing)."""
    role: list[Role | None] = [None]

    async def trip_access(user_id: str, trip_id: str) -> TripAccess | None:
        return TripAccess(trip_id=trip_id, owner="owner-1", role=role[0]) if role[0] else None

    async def trip_photo(owner: str, trip_id: str) -> tuple[bytes, str] | None:
        return (b"jpeg", "image/jpeg") if owner == "owner-1" else None

    monkeypatch.setattr(db, "trip_access", trip_access)
    monkeypatch.setattr(db, "trip_photo", trip_photo)
    web.app.dependency_overrides[web.current_user] = lambda: ME
    yield role
    web.app.dependency_overrides.clear()


@pytest.mark.parametrize("role", [None, Role.VIEW, Role.EDIT, Role.OWNER])
def test_too_little_access_looks_like_no_trip(as_role: list[Role | None], role: Role | None) -> None:
    as_role[0] = role
    resp = TestClient(web.app).get("/trips/t1/photo")
    # The photo is read as the trip's owner's, not the caller's.
    assert resp.status_code == (404 if role is None else 200)

    class Restate:
        async def object_call(self, *_: Any) -> None:
            pass

    web.app.dependency_overrides[web.rs] = Restate
    resp = TestClient(web.app, follow_redirects=False).post("/trips/t1/rename", data={"name": "Porto"})
    assert resp.status_code == (303 if role in (Role.EDIT, Role.OWNER) else 404)


def test_merging_needs_both_trips_to_be_yours(monkeypatch: pytest.MonkeyPatch) -> None:
    async def trip_access(user_id: str, trip_id: str) -> TripAccess | None:
        role = {"mine": Role.OWNER, "shared": Role.EDIT}.get(trip_id)
        return TripAccess(trip_id=trip_id, owner=user_id, role=role) if role else None

    class Restate:
        async def object_call(self, *_: Any) -> None:
            pass

    monkeypatch.setattr(db, "trip_access", trip_access)
    web.app.dependency_overrides[web.current_user] = lambda: ME
    web.app.dependency_overrides[web.rs] = Restate
    try:
        client = TestClient(web.app, follow_redirects=False)
        assert client.post("/trips/mine/merge", data={"into": "shared"}).status_code == 404
        assert client.post("/trips/shared/merge", data={"into": "mine"}).status_code == 404
    finally:
        web.app.dependency_overrides.clear()


@needs_db
@pytest.mark.anyio
async def test_the_owner_has_access_and_strangers_have_none(database: str) -> None:
    me, other = await db.user_for_email("me@example.com"), await db.user_for_email("them@example.com")
    hotel = Reservation(
        id=str(uuid.uuid4()),
        kind=Kind.LODGING,
        title="Hotel",
        start_local=datetime(2026, 11, 13, 15),
        city="Lisbon",
    )
    filed = await db.file_reservations(me, "mail-1", [hotel])
    (trip_id,) = filed.trip_ids
    assert await db.trip_access(me, trip_id) == TripAccess(trip_id=trip_id, owner=me, role=Role.OWNER)
    assert await db.trip_access(other, trip_id) is None
    assert await db.trip_access(me, "no-such-trip") is None
