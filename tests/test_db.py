"""Integration tests against a real Postgres (see conftest.py)."""

import os
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import restate

from triptych import db
from triptych.models import (
    InboxItem,
    IngestState,
    Kind,
    Notification,
    PhotoChoice,
    ReprocessRequest,
    Reservation,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]


@pytest.fixture
async def me(database: str) -> str:
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


def porto() -> Reservation:
    return res(Kind.LODGING, "2026-11-15T15:00", "2026-11-17T11:00", "Porto", "H4")


async def test_title_follows_the_itinerary_until_renamed(me: str) -> None:
    # The flight home arrives first: until there's a hotel, the trip is named for where it lands.
    [trip_id] = (await db.file_reservations(me, "ingest-home", [home()])).trip_ids
    assert [s.name for s in await db.trip_summaries(me)] == ["San Francisco, November 2026"]

    await db.file_reservations(me, "ingest-hotel", [hotel()])
    await db.file_reservations(me, "ingest-porto", [porto()])
    trip = await db.get_trip(me, trip_id)
    assert trip and (trip.name, trip.destination) == ("Lisbon & Porto, November 2026", "Lisbon")

    await db.rename_trip(me, trip_id, "Portugal with Sam")
    await db.file_reservations(me, "ingest-out", [outbound()])
    trip = await db.get_trip(me, trip_id)
    assert trip and (trip.name, len(trip.reservations)) == ("Portugal with Sam", 4)


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


def from_ingest(r: Reservation, ingest_id: str) -> Reservation:
    return r.model_copy(update={"source_ingest_id": ingest_id})


async def test_deleted_reservation_stays_deleted_when_its_ingest_is_filed_again(me: str) -> None:
    filed = await db.file_reservations(
        me, "mail-1", [from_ingest(hotel(), "mail-1"), from_ingest(porto(), "mail-1")]
    )
    [trip_id] = filed.trip_ids
    lisbon = next(f.reservation for f in filed.reservations if f.reservation.city == "Lisbon")
    assert await db.remove_reservation(me, trip_id, lisbon.id) is True

    # A re-extraction of the same email (new ids, the hotel retitled) brings back only Porto.
    retitled = from_ingest(hotel(), "mail-1").model_copy(update={"title": "Hotel Avenida"})
    again = await db.file_reservations(me, "mail-1", [retitled, from_ingest(porto(), "mail-1")])
    assert [f.reservation.city for f in again.reservations] == ["Porto"]

    # The same booking in a new email is the user sending it again: it's filed.
    resent = await db.file_reservations(me, "mail-2", [from_ingest(hotel(), "mail-2")])
    assert "Lisbon" in [f.reservation.city for f in resent.reservations]


async def test_deleted_trip_dismisses_everything_in_it(me: str) -> None:
    filed = await db.file_reservations(me, "mail-1", [from_ingest(hotel(), "mail-1")])
    await db.delete_trip(me, filed.trip_ids[0])
    again = await db.file_reservations(me, "mail-1", [from_ingest(hotel(), "mail-1")])
    assert again.trip_ids == [] and await db.trip_summaries(me) == []


def test_dismissal_matching() -> None:
    gone = db.Dismissal(
        dedupe_key=db.dedupe_key(hotel()), kind="lodging", confirmation_code="H3",
        start_date=hotel().start_date, city="Lisbon",
    )  # fmt: skip
    assert db.dismissed(hotel(), gone)
    assert db.dismissed(hotel().model_copy(update={"title": "Renamed", "city": None}), gone)  # same code
    assert db.dismissed(hotel().model_copy(update={"title": "Renamed", "confirmation_code": None}), gone)
    assert not db.dismissed(porto(), gone)
    assert not db.dismissed(outbound(), gone)


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


async def test_addresses_map_to_one_user_each(database: str) -> None:
    me = await db.user_for_email(" Me@Example.com ")
    assert await db.user_for_email("me@example.com") == me
    assert await db.user_for_email("someone@example.com") != me
    assert await db.primary_email(me) == "me@example.com"


async def test_writes_announce_whose_data_changed(database: str, me: str) -> None:
    feed = db.changes(database)
    assert await anext(feed) is None  # listening: the caller should resync
    other = await db.user_for_email("someone-else@example.com")

    item = InboxItem(
        ingest_id="i1", source="upload", label="a", received_at=datetime.now(UTC), state="queued"
    )
    await db.add_inbox_item(me, item)
    await db.add_inbox_item(me, item.model_copy(update={"state": "done"}))  # already there: no-op, no news
    await db.upsert_inbox(other, item.model_copy(update={"ingest_id": "i2", "state": "parsing"}))
    await db.dismiss_notifications(me)

    assert [await anext(feed) for _ in range(3)] == [me, other, me]
    [row] = await db.inbox(me)
    assert row.state == "queued"
    await feed.aclose()


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
    assert not any(i.has_original or i.has_run for i in await db.recent_ingests())  # nothing was kept
    [one] = await db.reprocess_candidates(ingest_id="b")
    assert (one.ingest_id, one.owner) == ("b", you)


async def test_trips_share_their_place_photo_once_stored(me: str) -> None:
    trip_id = (await db.file_reservations(me, "i1", [outbound(), hotel()])).trip_ids[0]
    assert await db.places_needing_photos([trip_id]) == ["Lisbon"]
    assert await db.place_photo_state("Lisbon") == db.PhotoState()
    assert (await db.trip_summaries(me))[0].photo is None
    assert await db.trip_photo(me, trip_id) is None

    choice = PhotoChoice(
        file="File:Lisbon.jpg",
        image_url="https://upload.wikimedia.org/Lisbon.jpg",
        page_url="https://commons.wikimedia.org/wiki/File:Lisbon.jpg",
        artist="Some One",
        licence="CC BY 4.0",
    )
    await db.save_place_photo("Lisbon", choice, b"jpeg", "image/jpeg", 1.5)
    # Stored, but its subject is still to be found.
    assert await db.place_photo_state("Lisbon") == db.PhotoState(looked_up=True, needs_subject=True)
    assert await db.places_needing_photos() == ["Lisbon"]
    assert await db.place_photo_image("Lisbon") == b"jpeg"
    photo = (await db.trip_summaries(me))[0].photo
    assert photo is not None and photo.artist == "Some One" and photo.version and photo.aspect == 1.5
    assert photo.subject is None
    trip = await db.get_trip(me, trip_id)
    assert trip is not None and trip.photo == photo and trip.name.startswith("Lisbon")
    assert await db.trip_photo(me, trip_id) == (b"jpeg", "image/jpeg")
    assert await db.trip_photo(await db.user_for_email("other@example.com"), trip_id) is None

    await db.save_photo_subject("Lisbon", [0.25, 0.0, 0.75, 0.5])
    assert await db.place_photo_state("Lisbon") == db.PhotoState(looked_up=True)
    assert await db.places_needing_photos() == []
    photo = (await db.trip_summaries(me))[0].photo
    assert photo is not None and photo.subject == [0.25, 0.0, 0.75, 0.5] and photo.aspect == 1.5

    # Nothing found is remembered, and leaves the trip without a photo.
    await db.save_place_photo("Lisbon", None, None, None)
    assert await db.places_needing_photos() == []
    assert (await db.trip_summaries(me))[0].photo is None


async def test_older_trips_get_their_place_filled_in(me: str) -> None:
    trip_id = (await db.file_reservations(me, "i1", [hotel()])).trip_ids[0]
    async with db.connection() as conn:
        await conn.execute("UPDATE trip SET place = NULL")
        await conn.commit()
    assert await db.places_needing_photos() == []
    await db.fill_places()
    assert await db.places_needing_photos([trip_id]) == ["Lisbon"]


async def test_older_reservations_get_their_timezone_refilled(me: str) -> None:
    train = res(Kind.RAIL, "2026-11-14T09:00", "2026-11-14T12:00", "Porto")
    trip_id = (await db.file_reservations(me, "i1", [outbound(), hotel(), train])).trip_ids[0]
    async with db.connection() as conn:
        await conn.execute("UPDATE reservation SET start_tz = NULL, end_tz = NULL WHERE kind = 'rail'")
        await conn.execute("UPDATE reservation SET start_tz = 'Europe/Madrid' WHERE kind = 'lodging'")
        await conn.commit()
    await db.refill_timezones()
    trip = await db.get_trip(me, trip_id)
    assert trip is not None
    stored = next(r for r in trip.reservations if r.kind is Kind.RAIL)
    assert (stored.start_tz, stored.end_tz) == ("Europe/Lisbon", "Europe/Lisbon")
    lodging = next(r for r in trip.reservations if r.kind is Kind.LODGING)
    assert (lodging.start_tz, lodging.end_tz) == ("Europe/Lisbon", "Europe/Lisbon")


async def test_older_trips_get_retitled_unless_renamed(me: str) -> None:
    ours = (await db.file_reservations(me, "i1", [outbound(), home()])).trip_ids[0]
    spring = res(Kind.LODGING, "2027-03-01T15:00", "2027-03-05T11:00", "Porto")
    theirs = (await db.file_reservations(me, "i2", [spring])).trip_ids[0]
    await db.rename_trip(me, theirs, "Spring break")
    async with db.connection() as conn:
        await conn.execute(
            "UPDATE trip SET destination = 'San Francisco', name = 'San Francisco' WHERE id = %s", (ours,)
        )
        await conn.commit()
    await db.retitle_trips()
    trip = await db.get_trip(me, ours)
    assert trip and (trip.name, trip.destination) == ("Lisbon, November 2026", "Lisbon")
    trip = await db.get_trip(me, theirs)
    assert trip and trip.name == "Spring break"


def test_reconcile_matches_one_to_one_and_reports_what_changed() -> None:
    lisbon, porto_, flight = (from_ingest(r, "mail-1") for r in (hotel(), porto(), outbound()))
    # The new run: the hotel retitled (same code and day), the flight a minute later (same code and
    # day), Porto gone, a dinner new, and the hotel twice.
    retitled = lisbon.model_copy(update={"id": "n1", "title": "Hotel Avenida"})
    later = flight.model_copy(update={"id": "n2", "start_local": flight.start_local + timedelta(minutes=1)})
    dinner = from_ingest(res(Kind.RESTAURANT, "2026-11-14T20:00", city="Lisbon"), "mail-1")
    again = retitled.model_copy(update={"id": "n3"})
    rec = db.reconcile([lisbon, porto_, flight], [retitled, later, dinner, again], [], set())

    assert [u.id for u in rec.updated] == [lisbon.id, flight.id]  # the existing ids are kept
    assert [(c.id, [f.field for f in c.fields]) for c in rec.changes] == [
        (lisbon.id, ["title"]),
        (flight.id, ["start_local"]),
    ]
    assert rec.changes[0].fields[0].new == "Hotel Avenida"
    assert rec.added == [dinner]
    assert rec.missing == [porto_]
    assert [(s.reservation.id, s.reason) for s in rec.skipped] == [("n3", "duplicate")]


def test_reconcile_skips_what_the_user_deleted() -> None:
    gone = db.Dismissal(
        dedupe_key="x", kind="lodging", confirmation_code="H3", start_date=hotel().start_date, city=None
    )
    rec = db.reconcile([], [hotel(), porto()], [gone], {db.dedupe_key(porto())})
    assert [(s.reservation.city, s.reason) for s in rec.skipped] == [
        ("Lisbon", "dismissed"),
        ("Porto", "duplicate"),
    ]
    assert rec.added == []


def test_reconcile_changes_the_kind_of_the_same_departure() -> None:
    """A run that knows a kind the old one didn't (a ferry filed as `other`) updates it in place."""
    old = from_ingest(res(Kind.OTHER, "2026-11-15T09:30", city="Lisbon", code="F7"), "mail-1")
    ferry = old.model_copy(update={"id": "n1", "kind": Kind.FERRY, "title": "Lisbon to Cacilhas"})
    other_code = ferry.model_copy(update={"id": "n2", "confirmation_code": "Z9"})
    rec = db.reconcile([old], [other_code, ferry], [], set())
    assert [u.id for u in rec.updated] == [old.id]
    assert rec.changes[0].fields[0] == db.FieldChange(field="kind", old="other", new="ferry")
    assert rec.missing == [] and rec.added == [other_code]  # a different booking code is a different one

    # An owner who set the kind themselves keeps it.
    kept = old.model_copy(update={"edited_fields": ["kind"]})
    assert db.reconcile([kept], [ferry], [], set()).updated[0].kind is Kind.OTHER


async def test_applying_a_reprocess_changes_the_kind_in_place(me: str) -> None:
    item = InboxItem(
        ingest_id="mail-2", source="email", label="x", received_at=datetime.now(UTC), state="done"
    )
    await db.upsert_inbox(me, item)
    old = from_ingest(res(Kind.OTHER, "2026-11-15T09:30", city="Lisbon", code="F7"), "mail-2")
    [stored] = (await db.file_reservations(me, "mail-2", [old])).reservations
    ferry = old.model_copy(update={"id": str(uuid.uuid4()), "kind": Kind.FERRY})
    done = await db.apply_reprocess(me, "mail-2", str(uuid.uuid4()), [ferry])
    [trip_id] = done.filed.trip_ids
    trip = await db.get_trip(me, trip_id)
    assert trip is not None
    assert [(r.id, r.kind) for r in trip.reservations] == [(stored.reservation.id, Kind.FERRY)]


async def test_applying_a_reprocess_updates_in_place_and_never_resurrects(me: str) -> None:
    item = InboxItem(
        ingest_id="mail-1", source="email", label="x", received_at=datetime.now(UTC), state="done"
    )
    await db.upsert_inbox(me, item)
    filed = await db.file_reservations(
        me, "mail-1", [from_ingest(r, "mail-1") for r in (outbound(), hotel(), home())]
    )
    [trip_id] = filed.trip_ids
    stored = {f.reservation.kind: f.reservation for f in filed.reservations}
    flight_back = next(f.reservation for f in filed.reservations if f.reservation.confirmation_code == "B2")
    assert await db.remove_reservation(me, trip_id, flight_back.id)

    # A better model: the hotel has an address, the return flight is back, and there's a dinner.
    run = [
        from_ingest(r, "mail-1").model_copy(update={"source_run_id": "00000000-0000-0000-0000-000000000001"})
        for r in (outbound(), hotel().model_copy(update={"address": "Av. da Liberdade 1"}), home(),
                  res(Kind.RESTAURANT, "2026-11-14T20:00", city="Lisbon"))
    ]  # fmt: skip
    run_id = str(uuid.uuid4())
    preview = await db.preview_reprocess(me, "mail-1", run)
    done = await db.apply_reprocess(me, "mail-1", run_id, run)
    for rec in (preview, done.reconciliation):
        assert [c.id for c in rec.changes] == [stored[Kind.LODGING].id]
        assert [s.reason for s in rec.skipped] == ["dismissed"]
        assert [r.kind for r in rec.added] == [Kind.RESTAURANT]

    trip = await db.get_trip(me, trip_id)
    assert trip is not None
    by_kind = {r.kind: r for r in trip.reservations}
    assert by_kind[Kind.LODGING].id == stored[Kind.LODGING].id  # reminders stay keyed to it
    assert by_kind[Kind.LODGING].address == "Av. da Liberdade 1"
    assert by_kind[Kind.LODGING].source_run_id == run_id
    assert Kind.RESTAURANT in by_kind
    assert "B2" not in [r.confirmation_code for r in trip.reservations]  # still deleted

    # Applying again changes nothing.
    again = await db.apply_reprocess(me, "mail-1", run_id, run)
    assert again.reconciliation.changes == [] and again.reconciliation.added == []
    assert len((await db.get_trip(me, trip_id) or trip).reservations) == len(trip.reservations)


async def test_reprocessing_a_failed_ingest_files_it(me: str) -> None:
    item = InboxItem(
        ingest_id="mail-9", source="email", label="x", received_at=datetime.now(UTC), state="failed"
    )
    await db.upsert_inbox(me, item)
    run_id = str(uuid.uuid4())
    done = await db.apply_reprocess(me, "mail-9", run_id, [from_ingest(hotel(), "mail-9")])
    [inbox] = await db.inbox(me)
    assert inbox.state == "done" and inbox.trip_ids == done.filed.trip_ids


async def test_reprocess_batches_summarise_their_diffs(me: str) -> None:
    item = InboxItem(
        ingest_id="mail-1", source="email", label="Lisbon", received_at=datetime.now(UTC), state="done"
    )
    await db.upsert_inbox(me, item)
    req = ReprocessRequest(batch_id="b1", ingest_id="mail-1", mode="dry_run", model="gemma4:31b")
    await db.add_reprocess(req, me)
    await db.add_reprocess(req, me)  # the workflow records it again; still one row
    [queued] = await db.reprocess_batches()
    assert (queued.total, queued.queued, queued.model) == (1, 1, "gemma4:31b")

    run_id = str(uuid.uuid4())
    await db.finish_reprocess(req.key, run_id, db.reconcile([], [hotel(), porto()], [], set()), None)
    [batch] = await db.reprocess_batches()
    assert (batch.queued, batch.added, batch.changed) == (0, 2, 0)
    [row] = await db.reprocess_batch("b1")
    assert (row.label, row.owner, row.state, row.run_id) == ("Lisbon", "me@example.com", "done", run_id)
    assert row.diff is not None and [r.city for r in row.diff.added] == ["Lisbon", "Porto"]
    assert await db.reprocess_runs("b1") == [("mail-1", me, run_id)]
