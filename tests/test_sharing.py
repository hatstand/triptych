"""Sharing a trip by link: what a link shows, and that revoking, expiry and merges do what they say."""

import logging
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime

import pytest
from fastapi.testclient import TestClient

from triptych import db, sharing, telemetry
from triptych.auth import Me
from triptych.models import Kind, Reservation, Trip
from triptych.web import app as web
from triptych.web import ratelimit

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def flight(**changes: object) -> Reservation:
    fields: dict[str, object] = dict(
        id=str(uuid.uuid4()), kind=Kind.FLIGHT, title="TP 1351 LIS→OPO", provider="TAP",
        confirmation_code="SECRET1", seat="12A", travelers=["Ada Lovelace"], notes="door code 4711",
        start_local=datetime(2026, 11, 14, 9, 0), end_local=datetime(2026, 11, 14, 10, 0),
        start_tz="Europe/Lisbon", end_tz="Europe/Lisbon", start_location="Lisbon (LIS)",
        end_location="Porto (OPO)", city="Porto", service_number="TP 1351", source_ingest_id="mail-1",
        edited_fields=["seat"],
    )  # fmt: skip
    return Reservation.model_validate(fields | changes)


def test_a_link_shows_the_plan_and_nothing_that_could_change_a_booking() -> None:
    trip = Trip(id="t1", owner="owner-1", name="Porto", reservations=[flight()])
    (shown,) = sharing.shared_view(trip).reservations
    assert shown.title == "TP 1351 LIS→OPO" and shown.service_number == "TP 1351"
    assert shown.start_location == "Lisbon (LIS)" and shown.end_tz == "Europe/Lisbon"
    assert (shown.confirmation_code, shown.seat, shown.travelers, shown.notes) == (None, None, [], None)
    assert (shown.source_ingest_id, shown.source_run_id, shown.edited_fields) == (None, None, [])
    assert sharing.shared_view(trip).owner == ""


def test_new_reservation_fields_stay_off_links_until_listed() -> None:
    # Adding a field to Reservation fails this until it's sorted into one of the two sets.
    hidden = {"confirmation_code", "seat", "travelers", "notes"} | {
        "source_ingest_id",
        "source_run_id",
        "edited_fields",
    }
    assert set(Reservation.model_fields) == sharing.SHARED_FIELDS | hidden
    assert not sharing.SHARED_FIELDS & hidden


def test_access_logs_hide_link_tokens() -> None:
    record = logging.LogRecord(
        "uvicorn.access", logging.INFO, __file__, 1, '%s - "%s %s HTTP/%s" %d',
        ("1.2.3.4", "GET", "/s/abcDEF_123-x/photo?v=1", "1.1", 200), None,
    )  # fmt: skip
    telemetry.RedactShareTokens().filter(record)
    assert record.getMessage() == '1.2.3.4 - "GET /s/<token>/photo?v=1 HTTP/1.1" 200'


# --- with a database -------------------------------------------------------------------------


@pytest.fixture
async def owner(database: str) -> str:
    return await db.user_for_email("me@example.com")


async def a_trip(owner: str, **changes: object) -> str:
    filed = await db.file_reservations(owner, f"mail-{uuid.uuid4()}", [flight(**changes)])
    (trip_id,) = filed.trip_ids
    return trip_id


@needs_db
@pytest.mark.anyio
async def test_a_link_opens_its_trip_until_revoked(owner: str) -> None:
    trip_id = await a_trip(owner)
    link_id = await sharing.create_link(owner, trip_id, " Mum ")
    assert link_id is not None
    (link,) = await sharing.links(owner, trip_id)
    assert (link.id, link.label, link.last_used_at) == (link_id, "Mum", None)
    assert link.token == sharing.token_for(link_id)

    found = await sharing.open_link(link.token)
    assert found == sharing.LinkedTrip(trip_id=trip_id, owner=owner)
    (link,) = await sharing.links(owner, trip_id)
    assert link.last_used_at is not None
    assert await sharing.open_link(link.token + "x") is None

    await sharing.revoke_link(owner, trip_id, link_id)
    assert await sharing.open_link(link.token) is None
    assert await sharing.links(owner, trip_id) == []


@needs_db
@pytest.mark.anyio
async def test_only_the_owner_makes_or_revokes_links(owner: str) -> None:
    other = await db.user_for_email("them@example.com")
    trip_id = await a_trip(owner)
    assert await sharing.create_link(other, trip_id, None) is None
    link_id = await sharing.create_link(owner, trip_id, None)
    assert link_id is not None
    await sharing.revoke_link(other, trip_id, link_id)
    assert await sharing.open_link(sharing.token_for(link_id)) is not None
    assert await sharing.links(other, trip_id) == []


@needs_db
@pytest.mark.anyio
async def test_an_expired_link_stops_working(owner: str) -> None:
    trip_id = await a_trip(owner)
    link_id = await sharing.create_link(owner, trip_id, None)
    async with db.connection() as conn:
        await conn.execute(
            "UPDATE share_link SET expires_at = now() - interval '1 minute' WHERE id = %s", (link_id,)
        )
    assert link_id and await sharing.open_link(sharing.token_for(link_id)) is None


@needs_db
@pytest.mark.anyio
async def test_merging_keeps_the_links_and_deleting_ends_them(owner: str) -> None:
    lisbon = await a_trip(owner)
    porto = await a_trip(owner, start_local=datetime(2027, 3, 1, 9, 0), end_local=datetime(2027, 3, 1, 10, 0))
    link_id = await sharing.create_link(owner, lisbon, "Mum")
    assert link_id is not None
    await db.merge_trips(owner, lisbon, porto)
    assert await sharing.open_link(sharing.token_for(link_id)) == sharing.LinkedTrip(
        trip_id=porto, owner=owner
    )
    await db.delete_trip(owner, porto)
    assert await sharing.open_link(sharing.token_for(link_id)) is None


@pytest.fixture
def client() -> Iterator[TestClient]:
    web.app.dependency_overrides[web.limiter] = ratelimit.limiter
    yield TestClient(web.app, follow_redirects=False)
    web.app.dependency_overrides.clear()


@needs_db
@pytest.mark.anyio
async def test_the_link_page_is_read_only_and_redacted(owner: str, client: TestClient) -> None:
    trip_id = await a_trip(owner)
    link_id = await sharing.create_link(owner, trip_id, None)
    assert link_id is not None
    resp = client.get(f"/s/{sharing.token_for(link_id)}")
    assert resp.status_code == 200
    assert "TP 1351 LIS→OPO" in resp.text and "TAP" in resp.text
    for secret in ("SECRET1", "12A", "Ada Lovelace", "door code", "/delete", "Ask about", "Sign out"):
        assert secret not in resp.text
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert resp.headers["x-robots-tag"] == "noindex, nofollow"
    assert resp.headers["cache-control"] == "private, no-store"
    assert client.get("/s/not-a-link").status_code == 404
    assert client.get("/s/not-a-link/photo").status_code == 404


@needs_db
@pytest.mark.anyio
async def test_the_owner_opening_their_own_link_gets_the_trip_page(owner: str, client: TestClient) -> None:
    trip_id = await a_trip(owner)
    link_id = await sharing.create_link(owner, trip_id, None)
    assert link_id is not None

    async def signed_in(_: object) -> Me:
        return Me(id=owner, email="me@example.com")

    original = web.signed_in
    web.signed_in = signed_in  # ty: ignore[invalid-assignment]
    try:
        resp = client.get(f"/s/{sharing.token_for(link_id)}")
    finally:
        web.signed_in = original
    assert (resp.status_code, resp.headers["location"]) == (303, f"/trips/{trip_id}")


@needs_db
@pytest.mark.anyio
async def test_the_owner_makes_and_revokes_links_from_the_trip_page(owner: str, client: TestClient) -> None:
    trip_id = await a_trip(owner)
    web.app.dependency_overrides[web.current_user] = lambda: Me(id=owner, email="me@example.com")
    assert client.post(f"/trips/{trip_id}/links", data={"label": "Host"}).status_code == 303
    (link,) = await sharing.links(owner, trip_id)
    page = client.get(f"/trips/{trip_id}").text
    assert f"/s/{link.token}" in page and "Host" in page
    assert client.post(f"/trips/{trip_id}/links/{link.id}/delete").status_code == 303
    assert await sharing.links(owner, trip_id) == []


@needs_db
@pytest.mark.anyio
async def test_a_link_can_expire_a_week_after_the_trip(owner: str) -> None:
    trip_id = await a_trip(owner)  # its flight lands 14 Nov 2026
    link_id = await sharing.create_link(owner, trip_id, None, expire_after_trip=True)
    assert link_id is not None
    async with db.connection() as conn:
        cur = await conn.execute("SELECT expires_at FROM share_link WHERE id = %s", (link_id,))
        row = await cur.fetchone()
    assert row is not None and row[0] == datetime(2026, 11, 22, tzinfo=UTC)
    (link,) = await sharing.links(owner, trip_id)
    assert link.last_day == date(2026, 11, 21)

    async with db.connection() as conn:
        await conn.execute("UPDATE share_link SET expires_at = now() WHERE id = %s", (link_id,))
    assert await sharing.links(owner, trip_id) == []
    assert await sharing.delete_expired_links() == 1


@needs_db
@pytest.mark.anyio
async def test_a_links_calendar_is_as_redacted_as_its_page(owner: str, client: TestClient) -> None:
    trip_id = await a_trip(owner)
    link_id = await sharing.create_link(owner, trip_id, None)
    assert link_id is not None
    resp = client.get(f"/s/{sharing.token_for(link_id)}/calendar.ics")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/calendar")
    assert resp.headers["referrer-policy"] == "no-referrer"
    assert "TP 1351 LIS" in resp.text and "TAP" in resp.text
    for secret in ("SECRET1", "12A", "Ada Lovelace", "door code"):
        assert secret not in resp.text
    await sharing.revoke_link(owner, trip_id, link_id)
    assert client.get(f"/s/{sharing.token_for(link_id)}/calendar.ics").status_code == 404
