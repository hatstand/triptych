"""Integration tests against a real Postgres (see conftest.py)."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import restate

from triptych import db
from triptych.models import InboxItem, IngestState, Kind, Notification, Reservation

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]


@pytest.fixture
async def me(database: None) -> str:
    return await db.user_for_email("me@example.com")


def res(
    kind: Kind,
    start: str,
    end: str | None = None,
    city: str | None = None,
    code: str | None = None,
    tz: str | None = None,
    end_tz: str | None = None,
) -> Reservation:
    return Reservation(
        id=str(uuid.uuid4()),
        kind=kind,
        title=f"{kind.value} {start}",
        confirmation_code=code,
        start_local=datetime.fromisoformat(start),
        start_tz=tz,
        end_local=datetime.fromisoformat(end) if end else None,
        end_tz=end_tz,
        city=city,
    )


def outbound() -> Reservation:
    return res(Kind.FLIGHT, "2026-11-12T15:40", "2026-11-13T11:25", "Lisbon", "A1",
               "America/Los_Angeles", "Europe/Lisbon")  # fmt: skip


def home() -> Reservation:
    return res(Kind.FLIGHT, "2026-11-17T13:05", "2026-11-17T16:50", "San Francisco", "B2",
               "Europe/Lisbon", "America/Los_Angeles")  # fmt: skip


def hotel() -> Reservation:
    return res(Kind.LODGING, "2026-11-13T15:00", "2026-11-17T11:00", "Lisbon", "H3")


async def test_filing_creates_trip_and_inherits_timezone(me: str) -> None:
    filed = await db.file_reservations(me, "ingest-1", [outbound(), hotel()])
    assert len(filed.trip_ids) == 1
    [summary] = await db.trip_summaries(me)
    assert (summary.name, summary.reservation_count) == ("Lisbon, November 2026", 2)
    stay = next(f.reservation for f in filed.reservations if f.reservation.kind is Kind.LODGING)
    assert stay.start_tz == "Europe/Lisbon"


async def test_filing_is_idempotent_when_restate_retries(me: str) -> None:
    rs = [outbound(), hotel()]
    first = await db.file_reservations(me, "ingest-1", rs)
    again = await db.file_reservations(me, "ingest-1", rs)
    assert first == again
    [summary] = await db.trip_summaries(me)
    assert summary.reservation_count == 2


async def test_bridging_hotel_merges_trips_and_repoints_references(me: str) -> None:
    a = await db.file_reservations(me, "ingest-out", [outbound()])
    b = await db.file_reservations(me, "ingest-home", [home()])
    assert a.trip_ids != b.trip_ids
    item = InboxItem(
        ingest_id="ingest-home", source="email", label="home", received_at=datetime.now(UTC), state="done",
        trip_ids=b.trip_ids,
    )  # fmt: skip
    await db.upsert_inbox(me, item)
    note = Notification(id=str(uuid.uuid4()), at=datetime.now(UTC), trip_id=b.trip_ids[0], text="x")
    await db.add_notification(me, note)

    await db.file_reservations(me, "ingest-hotel", [hotel()])

    [summary] = await db.trip_summaries(me)
    assert summary.reservation_count == 3
    [item] = await db.inbox(me)
    assert item.trip_ids == [summary.id]
    [n] = await db.notifications(me)
    assert n.trip_id == summary.id


async def test_resent_confirmation_replaces_segment_keeping_its_id(me: str) -> None:
    first = await db.file_reservations(me, "ingest-1", [hotel()])
    again = await db.file_reservations(me, "ingest-2", [hotel()])  # same booking, new ids
    [summary] = await db.trip_summaries(me)
    assert summary.reservation_count == 1
    # Stable id → same reminder idempotency keys → Restate doesn't schedule duplicates.
    assert again.reservations[0].reservation.id == first.reservations[0].reservation.id


async def test_ownership_and_last_reservation_deletes_trip(me: str) -> None:
    filed = await db.file_reservations(me, "ingest-1", [hotel()])
    [trip_id] = filed.trip_ids
    rid = filed.reservations[0].reservation.id
    with pytest.raises(restate.TerminalError):
        await db.remove_reservation(await db.user_for_email("someone@else.com"), trip_id, rid)
    assert await db.remove_reservation(me, trip_id, rid) is False
    assert await db.trip_summaries(me) == []
    assert await db.get_trip(me, trip_id) is None


async def test_reminder_target_is_empty_once_rescheduled(me: str) -> None:
    filed = await db.file_reservations(me, "ingest-1", [outbound()])
    r = filed.reservations[0].reservation
    assert (await db.reminder_target(r.id, r.start_local)).owner == me
    assert (await db.reminder_target(r.id, datetime(2026, 1, 1))).owner is None


async def test_all_trips_feed_query(me: str) -> None:
    await db.file_reservations(me, "ingest-1", [outbound(), hotel(), home()])
    await db.file_reservations(me, "ingest-2", [res(Kind.RAIL, "2027-01-21T08:31", city="Paris")])
    trips = await db.all_trips(me)
    assert sorted(len(t.reservations) for t in trips) == [1, 3]


async def test_addresses_map_to_one_user_each(database: None) -> None:
    me = await db.user_for_email(" Me@Example.com ")
    assert await db.user_for_email("me@example.com") == me
    assert await db.user_for_email("someone@example.com") != me
    assert await db.primary_email(me) == "me@example.com"


async def test_admin_ingest_queries_span_users(me: str) -> None:
    you = await db.user_for_email("you@example.com")
    now = datetime.now(UTC)
    rows: list[tuple[str, str, IngestState, datetime]] = [
        (me, "a", "done", now),
        (you, "b", "failed", now - timedelta(hours=1)),
        (me, "old", "failed", now - timedelta(days=30)),
    ]
    for owner, ingest_id, state, at in rows:
        item = InboxItem(ingest_id=ingest_id, source="email", label=ingest_id, received_at=at, state=state)
        await db.upsert_inbox(owner, item)

    assert await db.ingest_counts(now - timedelta(days=7)) == {"done": 1, "failed": 1}
    assert [(i.ingest_id, i.owner) for i in await db.recent_ingests()] == [
        ("a", "me@example.com"),
        ("b", "you@example.com"),
        ("old", "me@example.com"),
    ]
    assert [i.ingest_id for i in await db.recent_ingests("failed")] == ["b", "old"]
