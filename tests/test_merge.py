"""Merging one trip into another (a trip card dropped on another), and the merge lasting."""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, date, datetime
from typing import Any

import pytest
import restate
from fastapi.testclient import TestClient

from triptych import db
from triptych.auth import Me
from triptych.models import InboxItem, Kind, MergeTrips, Reservation, Trip, TripSummary
from triptych.web import app as web
from triptych.web import ratelimit

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


@pytest.fixture
async def me(database: str) -> str:
    return await db.user_for_email("me@example.com")


def res(kind: Kind, start: str, end: str | None, city: str, code: str, ingest: str) -> Reservation:
    return Reservation(
        id=str(uuid.uuid4()),
        kind=kind,
        title=f"{kind.value} {start}",
        confirmation_code=code,
        start_local=datetime.fromisoformat(start),
        end_local=datetime.fromisoformat(end) if end else None,
        city=city,
        source_ingest_id=ingest,
    )


def lisbon() -> Reservation:
    return res(Kind.LODGING, "2026-11-13T15:00", "2026-11-17T11:00", "Lisbon", "H1", "mail-lisbon")


def porto() -> Reservation:
    """Three days after Lisbon: too far apart for filing to put them in one trip."""
    return res(Kind.LODGING, "2026-11-20T15:00", "2026-11-22T11:00", "Porto", "H2", "mail-porto")


async def two_trips(me: str) -> tuple[str, str]:
    [a] = (await db.file_reservations(me, "mail-lisbon", [lisbon()])).trip_ids
    [b] = (await db.file_reservations(me, "mail-porto", [porto()])).trip_ids
    assert a != b
    return a, b


@needs_db
@pytest.mark.anyio
async def test_merging_moves_everything_and_retitles(me: str) -> None:
    lisbon_trip, porto_trip = await two_trips(me)
    item = InboxItem(
        ingest_id="mail-porto", source="email", label="x", received_at=datetime.now(UTC), state="done",
        trip_ids=[porto_trip],
    )  # fmt: skip
    await db.upsert_inbox(me, item)

    await db.merge_trips(me, porto_trip, lisbon_trip)

    [summary] = await db.trip_summaries(me)
    assert summary.id == lisbon_trip
    assert (summary.name, summary.reservation_count) == ("Lisbon & Porto, November 2026", 2)
    assert (summary.start_date, summary.end_date) == (date(2026, 11, 13), date(2026, 11, 22))
    [inbox] = await db.inbox(me)
    assert inbox.trip_ids == [lisbon_trip]

    # Restate retrying the step after it committed finds nothing left to do.
    await db.merge_trips(me, porto_trip, lisbon_trip)
    assert [s.reservation_count for s in await db.trip_summaries(me)] == [2]


@needs_db
@pytest.mark.anyio
async def test_a_renamed_trip_keeps_its_name_whichever_way_its_merged(me: str) -> None:
    lisbon_trip, porto_trip = await two_trips(me)
    await db.rename_trip(me, porto_trip, "Portugal with Sam")
    await db.merge_trips(me, porto_trip, lisbon_trip)
    [summary] = await db.trip_summaries(me)
    assert (summary.id, summary.name) == (lisbon_trip, "Portugal with Sam")

    # Now that it's named, later bookings don't retitle it.
    await db.file_reservations(
        me, "mail-dinner", [res(Kind.RESTAURANT, "2026-11-21T20:00", None, "Porto", "D1", "mail-dinner")]
    )
    assert [s.name for s in await db.trip_summaries(me)] == ["Portugal with Sam"]


@needs_db
@pytest.mark.anyio
async def test_the_merge_survives_refiling_reprocessing_and_new_bookings(me: str) -> None:
    lisbon_trip, porto_trip = await two_trips(me)
    await db.merge_trips(me, porto_trip, lisbon_trip)

    # The same Porto email again, and a re-extraction of it, stay in the merged trip.
    await db.file_reservations(me, "mail-porto", [porto()])
    await db.apply_reprocess(
        me, "mail-porto", str(uuid.uuid4()), [porto().model_copy(update={"address": "Rua das Flores 1"})]
    )
    # So does a booking in the gap between the two that filing alone would have kept apart.
    train = res(Kind.RAIL, "2026-11-18T10:00", "2026-11-18T13:00", "Porto", "T1", "mail-train")
    await db.file_reservations(me, "mail-train", [train])

    [summary] = await db.trip_summaries(me)
    assert (summary.id, summary.reservation_count) == (lisbon_trip, 3)


@needs_db
@pytest.mark.anyio
async def test_only_your_own_trips_merge_and_never_into_themselves(me: str) -> None:
    lisbon_trip, porto_trip = await two_trips(me)
    them = await db.user_for_email("them@example.com")
    with pytest.raises(restate.TerminalError):
        await db.merge_trips(them, porto_trip, lisbon_trip)
    with pytest.raises(restate.TerminalError):
        await db.merge_trips(me, lisbon_trip, lisbon_trip)
    # Someone else's trip as the source is as good as gone: nothing moves.
    [theirs] = (await db.file_reservations(them, "mail-x", [porto()])).trip_ids
    await db.merge_trips(me, theirs, lisbon_trip)
    assert len(await db.trip_summaries(me)) == 2
    assert len(await db.trip_summaries(them)) == 1


# --- web -------------------------------------------------------------------------------------

ME = Me(id="user-1", email="me@example.com")


class FakeRestate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def object_call(self, _handler: Any, key: str, req: Any) -> None:
        self.calls.append((key, req))


@pytest.fixture
def restate_client(own_every_trip: None) -> Iterator[FakeRestate]:
    fake = FakeRestate()
    web.app.dependency_overrides[web.rs] = lambda: fake
    web.app.dependency_overrides[web.current_user] = lambda: ME
    web.app.dependency_overrides[web.limiter] = ratelimit.limiter
    yield fake
    web.app.dependency_overrides.clear()


@pytest.fixture
def client(restate_client: FakeRestate) -> TestClient:
    return TestClient(web.app, follow_redirects=False)


def test_the_merge_is_sent_to_the_user_object(client: TestClient, restate_client: FakeRestate) -> None:
    resp = client.post("/trips/t2/merge", data={"into": "t1"})
    assert resp.status_code == 303 and resp.headers["location"] == "/trips/t1"
    assert restate_client.calls == [(ME.id, MergeTrips(trip_id="t2", into="t1"))]


def summary(trip_id: str, name: str) -> TripSummary:
    return TripSummary(
        id=trip_id, name=name, destination=None, start_date=date(2026, 11, 13), end_date=date(2026, 11, 17),
        reservation_count=1,
    )  # fmt: skip


def test_the_trip_page_offers_the_other_trips_to_merge_into(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def get_trip(owner: str, trip_id: str) -> Trip:
        return Trip(id=trip_id, owner=owner, name="Lisbon", reservations=[lisbon()])

    async def trip_summaries(owner: str) -> list[TripSummary]:
        return [summary("t1", "Lisbon"), summary("t2", "Porto")]

    async def trip_sources(owner: str, trip_id: str) -> list[Any]:
        return []

    monkeypatch.setattr(web.db, "get_trip", get_trip)
    monkeypatch.setattr(web.db, "trip_summaries", trip_summaries)
    monkeypatch.setattr(web.db, "trip_sources", trip_sources)
    html = client.get("/trips/t1").text
    assert 'action="/trips/t1/merge"' in html
    assert '<option value="t2">Porto (13 Nov 2026)</option>' in html
    assert 'value="t1"' not in html


def test_home_trip_cards_can_be_dropped_on_each_other(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def trip_summaries(owner: str) -> list[TripSummary]:
        return [summary("t1", "Lisbon & Porto")]

    async def nothing(owner: str) -> list[Any]:
        return []

    monkeypatch.setattr(web.db, "trip_summaries", trip_summaries)
    monkeypatch.setattr(web.db, "inbox", nothing)
    monkeypatch.setattr(web.db, "notifications", nothing)
    html = client.get("/").text
    assert 'draggable="true" data-trip-id="t1" data-trip-name="Lisbon &amp; Porto"' in html
    assert '<dialog id="merge"' in html
