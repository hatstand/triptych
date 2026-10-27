"""Correcting a reservation on the trip page, and the corrections surviving what comes after."""

import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient

from triptych import db, enrich
from triptych.auth import Me
from triptych.models import (
    EditReservation,
    ExtractedReservation,
    Extraction,
    ExtractionRun,
    InboxItem,
    Kind,
    Reservation,
    ReservationFields,
    Trip,
    TripSummary,
)
from triptych.web import app as web
from triptych.web import ratelimit

needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")


def flight() -> Reservation:
    return Reservation(
        id=str(uuid.uuid4()), kind=Kind.FLIGHT, title="TP 1351 LIS→OPO", confirmation_code="F1",
        start_local=datetime(2026, 11, 14, 9, 0), end_local=datetime(2026, 11, 14, 10, 0),
        start_location="Lisbon (LIS)", end_location="Porto (OPO)", city="Porto",
        start_tz="Europe/Lisbon", end_tz="Europe/Lisbon", source_ingest_id="mail-1",
    )  # fmt: skip


def hotel() -> Reservation:
    return Reservation(
        id=str(uuid.uuid4()), kind=Kind.LODGING, title="Hotel Avenida", confirmation_code="H1",
        start_local=datetime(2026, 11, 13, 15, 0), end_local=datetime(2026, 11, 14, 11, 0),
        city="Lisbon", country_code="PT", source_ingest_id="mail-1",
    )  # fmt: skip


def corrected(r: Reservation, **changes: Any) -> ReservationFields:
    return ReservationFields.model_validate(r.model_dump() | changes)


# --- db --------------------------------------------------------------------------------------


@pytest.fixture
async def me(database: str) -> str:
    return await db.user_for_email("me@example.com")


async def stored(me: str, trip_id: str) -> dict[Kind, Reservation]:
    trip = await db.get_trip(me, trip_id)
    assert trip is not None
    return {r.kind: r for r in trip.reservations}


@needs_db
@pytest.mark.anyio
async def test_an_edit_records_what_changed_and_resorts_the_trip(me: str) -> None:
    filed = await db.file_reservations(me, "mail-1", [hotel(), flight()])
    [trip_id] = filed.trip_ids
    by_kind = await stored(me, trip_id)
    # The flight was really the day before, in the afternoon, so it comes before the hotel now.
    fix = corrected(by_kind[Kind.FLIGHT], start_local=datetime(2026, 11, 13, 13, 0),
                    end_local=datetime(2026, 11, 13, 14, 0), seat="12A")  # fmt: skip
    again = await db.edit_reservation(me, trip_id, by_kind[Kind.FLIGHT].id, fix)
    assert again is not None  # the trip as stored, for new reminders

    trip = await db.get_trip(me, trip_id)
    assert trip is not None
    assert [r.kind for r in trip.reservations] == [Kind.FLIGHT, Kind.LODGING]
    edited = trip.reservations[0]
    assert (edited.start_local, edited.seat) == (datetime(2026, 11, 13, 13, 0), "12A")
    assert edited.edited_fields == ["end_local", "seat", "start_local"]
    assert edited.id == by_kind[Kind.FLIGHT].id  # it keys the reminders

    # Saving the form again unchanged changes nothing.
    assert await db.edit_reservation(me, trip_id, edited.id, corrected(edited)) is None


@needs_db
@pytest.mark.anyio
async def test_refiling_the_same_booking_keeps_the_corrections(me: str) -> None:
    [trip_id] = (await db.file_reservations(me, "mail-1", [hotel()])).trip_ids
    original = (await stored(me, trip_id))[Kind.LODGING]
    await db.edit_reservation(me, trip_id, original.id, corrected(original, title="Casa do Miradouro"))

    # The same confirmation sent again, now with an address, under the extracted name.
    resent = hotel().model_copy(update={"address": "Rua da Saudade 1", "source_ingest_id": "mail-2"})
    await db.file_reservations(me, "mail-2", [resent])
    [r] = (await stored(me, trip_id)).values()
    assert (r.id, r.title, r.address) == (original.id, "Casa do Miradouro", "Rua da Saudade 1")


@needs_db
@pytest.mark.anyio
async def test_a_zone_its_owner_set_isnt_refilled(me: str) -> None:
    [trip_id] = (await db.file_reservations(me, "mail-1", [hotel()])).trip_ids
    r = (await stored(me, trip_id))[Kind.LODGING]
    assert r.start_tz == "Europe/Lisbon"  # from the city
    await db.edit_reservation(me, trip_id, r.id, corrected(r, start_tz="Atlantic/Madeira"))
    await db.refill_timezones()
    r = (await stored(me, trip_id))[Kind.LODGING]
    assert (r.start_tz, r.end_tz) == ("Atlantic/Madeira", "Europe/Lisbon")


@needs_db
@pytest.mark.anyio
async def test_reprocessing_keeps_the_corrections_and_finds_the_corrected_one(me: str) -> None:
    await db.upsert_inbox(
        me,
        InboxItem(ingest_id="mail-1", source="email", label="x", received_at=datetime.now(UTC), state="done"),
    )
    [trip_id] = (await db.file_reservations(me, "mail-1", [hotel(), flight()])).trip_ids
    f = (await stored(me, trip_id))[Kind.FLIGHT]
    # Every key the run could be matched on corrected: the code, the day and the number.
    fix = corrected(f, confirmation_code="ABC123", start_local=datetime(2026, 11, 15, 9, 0),
                    end_local=datetime(2026, 11, 15, 10, 0), service_number="TP 1351")  # fmt: skip
    await db.edit_reservation(me, trip_id, f.id, fix)

    # The same mistakes again, but a terminal this time.
    run = [hotel(), flight().model_copy(update={"notes": "Terminal 1"})]
    rec = await db.preview_reprocess(me, "mail-1", run)
    assert rec.added == [] and rec.missing == []
    assert [(c.id, [x.field for x in c.fields]) for c in rec.changes] == [(f.id, ["notes"])]

    await db.apply_reprocess(me, "mail-1", str(uuid.uuid4()), run)
    by_kind = await stored(me, trip_id)
    assert len(by_kind) == 2
    r = by_kind[Kind.FLIGHT]
    assert (r.confirmation_code, r.start_local, r.notes) == (
        "ABC123",
        datetime(2026, 11, 15, 9, 0),
        "Terminal 1",
    )
    # Applying the same run again still finds it.
    again = await db.apply_reprocess(me, "mail-1", str(uuid.uuid4()), run)
    assert again.reconciliation.added == [] and again.reconciliation.changes == []


@needs_db
@pytest.mark.anyio
async def test_a_corrected_city_drops_the_extracted_country(me: str) -> None:
    [trip_id] = (await db.file_reservations(me, "mail-1", [hotel()])).trip_ids
    r = (await stored(me, trip_id))[Kind.LODGING]
    assert r.country_code == "PT"
    await db.edit_reservation(me, trip_id, r.id, corrected(r, city="Vigo"))
    r = (await stored(me, trip_id))[Kind.LODGING]
    assert (r.city, r.country_code, r.edited_fields) == ("Vigo", None, ["city"])
    # Filing the booking again doesn't bring the old city's country back.
    await db.file_reservations(me, "mail-2", [hotel().model_copy(update={"source_ingest_id": "mail-2"})])
    assert (await stored(me, trip_id))[Kind.LODGING].country_code is None


@needs_db
@pytest.mark.anyio
async def test_countries_are_filled_in_from_the_run(me: str) -> None:
    await db.upsert_inbox(
        me,
        InboxItem(ingest_id="mail-1", source="email", label="x", received_at=datetime.now(UTC), state="done"),
    )
    extracted = ExtractedReservation(
        kind=Kind.LODGING, title="Hotel Avenida", confirmation_code="H1", start_local="2026-11-13T15:00",
        end_local="2026-11-14T11:00", city="Lisbon", country_code="PT",
    )  # fmt: skip
    output = Extraction(is_travel_document=True, reservations=[extracted])
    run_id = str(uuid.uuid4())
    await db.add_extraction_run(
        ExtractionRun(
            id=run_id, ingest_id="mail-1", owner=me, input_id=None, model="m", model_digest=None,
            prompt_version="p", code_version="dev", params={}, purpose="ingest", output=output,
        )
    )  # fmt: skip
    # Filed before reservations kept their country.
    old = hotel().model_copy(update={"country_code": None, "source_run_id": run_id})
    [trip_id] = (await db.file_reservations(me, "mail-1", [old])).trip_ids
    assert (await stored(me, trip_id))[Kind.LODGING].country_code is None
    await db.fill_countries()
    assert (await stored(me, trip_id))[Kind.LODGING].country_code == "PT"


def test_fill_timezones_leaves_what_the_owner_set() -> None:
    # The owner says the train left from Madrid; the hotel after it takes the train's arrival.
    train = Reservation(
        id="t", kind=Kind.RAIL, title="AVE", start_local=datetime(2026, 11, 14, 9, 0),
        end_local=datetime(2026, 11, 14, 12, 0), start_tz="Europe/Madrid", end_tz=None,
        start_location="Lisbon (LIS)", edited_fields=["start_tz", "end_tz"],
    )  # fmt: skip
    stay = Reservation(id="h", kind=Kind.LODGING, title="Hotel", start_local=datetime(2026, 11, 14, 15, 0))
    enrich.fill_timezones([train, stay])
    assert (train.start_tz, train.end_tz) == ("Europe/Madrid", None)
    assert stay.start_tz == "Europe/Madrid"


# --- web -------------------------------------------------------------------------------------

ME = Me(id="user-1", email="me@example.com")


class FakeRestate:
    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []

    async def object_call(self, _handler: Any, key: str, req: Any) -> None:
        self.calls.append((key, req))


@pytest.fixture
def restate_client() -> Iterator[FakeRestate]:
    fake = FakeRestate()
    web.app.dependency_overrides[web.rs] = lambda: fake
    web.app.dependency_overrides[web.current_user] = lambda: ME
    web.app.dependency_overrides[web.limiter] = ratelimit.limiter
    yield fake
    web.app.dependency_overrides.clear()


@pytest.fixture
def client(restate_client: FakeRestate) -> TestClient:
    return TestClient(web.app, follow_redirects=False)


FORM = {
    "kind": "flight", "title": " TP 1351 ", "provider": "", "confirmation_code": "F1",
    "start_local": "2026-11-14T09:00", "start_tz": "Europe/Lisbon", "end_local": "", "end_tz": "",
    "start_location": "Lisbon (LIS)", "end_location": "Porto (OPO)", "address": "", "city": "Porto",
    "service_number": "", "seat": "", "travelers": "Ana Silva\n\n Sam Jones \n", "notes": "",
}  # fmt: skip


def test_the_form_is_sent_to_the_user_object(client: TestClient, restate_client: FakeRestate) -> None:
    resp = client.post("/trips/t1/reservations/r1/edit", data=FORM)
    assert resp.status_code == 303 and resp.headers["location"] == "/trips/t1"
    [(key, req)] = restate_client.calls
    assert key == ME.id and isinstance(req, EditReservation)
    assert (req.trip_id, req.reservation_id) == ("t1", "r1")
    f = req.fields
    assert (f.title, f.provider, f.end_local, f.end_tz) == ("TP 1351", None, None, None)
    assert f.travelers == ["Ana Silva", "Sam Jones"]


@pytest.mark.parametrize(
    "changes",
    [
        {"start_tz": "Lisbon"},  # not a zone
        {"end_local": "2026-11-14T08:00", "end_tz": "Europe/Lisbon"},  # before it starts
        {"title": "  "},
    ],
)
def test_the_form_refuses_what_cant_be_right(
    client: TestClient, restate_client: FakeRestate, changes: dict[str, str]
) -> None:
    assert client.post("/trips/t1/reservations/r1/edit", data=FORM | changes).status_code == 422
    assert restate_client.calls == []


def test_an_arrival_earlier_on_the_clock_is_fine_across_zones(
    client: TestClient, restate_client: FakeRestate
) -> None:
    # Tokyo 21:00 → Honolulu 09:30 the same morning.
    tokyo = FORM | {"start_local": "2026-11-14T21:00", "start_tz": "Asia/Tokyo",
                    "end_local": "2026-11-14T09:30", "end_tz": "Pacific/Honolulu"}  # fmt: skip
    assert client.post("/trips/t1/reservations/r1/edit", data=tokyo).status_code == 303


def test_each_reservation_has_an_edit_dialog(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    rs = [hotel(), flight().model_copy(update={"edited_fields": ["seat"]})]

    async def get_trip(owner: str, trip_id: str) -> Trip:
        return Trip(id=trip_id, owner=owner, name="Lisbon", reservations=rs)

    async def trip_summaries(owner: str) -> list[TripSummary]:
        return []

    async def trip_sources(owner: str, trip_id: str) -> list[db.TripSource]:
        return [db.TripSource(ingest_id="mail-1", label="Your Iberia flight")]

    monkeypatch.setattr(web.db, "get_trip", get_trip)
    monkeypatch.setattr(web.db, "trip_summaries", trip_summaries)
    monkeypatch.setattr(web.db, "trip_sources", trip_sources)
    html = client.get("/trips/t1").text
    # Something extraction missed can be added, saying which email it was in.
    assert 'commandfor="add-reservation" command="show-modal"' in html
    assert 'action="/trips/t1/reservations"' in html
    assert '<option value="mail-1">Your Iberia flight</option>' in html
    for r in rs:
        assert f'commandfor="edit-{r.id}" command="show-modal"' in html
        assert f'action="/trips/t1/reservations/{r.id}/edit"' in html
    assert 'value="2026-11-14T09:00"' in html
    assert 'Seat <span class="muted">· edited</span>' in html
    assert '<option value="Europe/Lisbon">' in html
