"""Worked examples for extraction: the owner's corrections, kept for their own later mail, and only
what may be taught of them (moto stands in for R2)."""

import dataclasses
import os
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import boto3
import pytest
from moto import mock_aws

from triptych import db, enrich, examples, inputs
from triptych.config import settings
from triptych.inputs import InputStore
from triptych.models import (
    ExtractedReservation,
    Extraction,
    ExtractionRun,
    InboxItem,
    Kind,
    ReservationFields,
)

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

INPUTS = "triptych-inputs"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
EMAIL = (
    b"From: Iberia <noreply@Iberia.com>\r\nSubject: Your flight\r\n\r\n"
    b"Madrid to Lisbon, 3 Nov, IB 3106 dep 09:00. Booking ref ABC-123, or QX7 if you changed it.\r\n"
)
TRIP = Extraction(
    is_travel_document=True,
    reservations=[
        ExtractedReservation(
            kind=Kind.FLIGHT, title="IB 3106 MAD→LIS", confirmation_code="ABC123", service_number="IB 3106",
            start_local="2026-11-03T09:00", start_iata="MAD", end_iata="LIS", city="Lisbon",
            country_code="PT",
        ),
        ExtractedReservation(
            kind=Kind.LODGING, title="Hotel Avenida", start_local="2026-11-03T15:00", city="Lisbon",
        ),
    ],
)  # fmt: skip


@pytest.fixture
def store() -> Iterator[InputStore]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=INPUTS)
        s = dataclasses.replace(
            settings,
            inputs_bucket=INPUTS,
            inputs_master_keyset=inputs.new_master_keyset(),
            inputs_access_key_id="test",
            inputs_secret_access_key="test",
        )
        st = InputStore.from_settings(s)
        assert st is not None
        st.s3 = client
        yield st


@pytest.fixture
async def me(database: str) -> str:
    return await db.user_for_email("me@example.com")


async def filed(store: InputStore, owner: str, ingest_id: str = "mail-1") -> tuple[str, dict[Kind, Any]]:
    """An ingest as Ingest leaves it, its reservations filed: the trip, and them by kind."""
    await db.add_inbox_item(
        owner,
        InboxItem(ingest_id=ingest_id, source="email", label="Your flight", received_at=NOW, state="done"),
    )
    kept = await store.keep(
        owner, EMAIL, ingest_id=ingest_id, source="email", content_type="message/rfc822", filename=None,
        received_at=NOW,
    )  # fmt: skip
    run_id = str(uuid.uuid5(uuid.NAMESPACE_URL, ingest_id))
    await db.add_extraction_run(
        ExtractionRun(
            id=run_id, ingest_id=ingest_id, owner=owner, input_id=kept.id, model="gemma4:26b-a4b-it-qat",
            model_digest=None, prompt_version="0123456789ab", code_version="dev", params={},
            purpose="ingest", output=TRIP,
        )
    )  # fmt: skip
    made = [
        r
        for x in TRIP.reservations
        if (r := enrich.normalise(x, reservation_id=str(uuid.uuid4()), ingest_id=ingest_id, run_id=run_id))
    ]
    [trip_id] = (await db.file_reservations(owner, ingest_id, made)).trip_ids
    trip = await db.get_trip(owner, trip_id)
    assert trip is not None
    return trip_id, {r.kind: r for r in trip.reservations}


async def edit(owner: str, trip_id: str, r: Any, **changes: Any) -> None:
    await db.edit_reservation(
        owner, trip_id, r.id, ReservationFields.model_validate(r.model_dump() | changes)
    )


async def example(store: InputStore, ingest_id: str = "mail-1") -> db.Example | None:
    case = await db.ingest_case(ingest_id)
    assert case is not None
    return examples.build(case, await store.load(case.input))


async def test_an_edit_becomes_the_answer_with_only_what_it_may_teach(store: InputStore, me: str) -> None:
    trip_id, by_kind = await filed(store, me)
    assert await example(store) is None  # nothing corrected, nothing to teach

    flight = by_kind[Kind.FLIGHT]
    await edit(me, trip_id, flight, start_local=datetime(2026, 11, 3, 10, 15), title="Off to Lisbon")
    ex = await example(store)
    assert ex is not None
    first = ex.answer.reservations[0]
    assert first.start_local == "2026-11-03T10:15"
    assert first.title == "IB 3106 MAD→LIS"  # how they like it shown isn't what the email says
    assert (ex.sender_domain, ex.subject, ex.partial) == ("iberia.com", "Your flight", False)
    assert "Booking ref ABC-123" in ex.text
    assert ex.changes == ["IB 3106 MAD→LIS: start_local"]


async def test_a_code_the_email_doesnt_contain_isnt_taught(store: InputStore, me: str) -> None:
    trip_id, by_kind = await filed(store, me)
    await edit(me, trip_id, by_kind[Kind.FLIGHT], confirmation_code="ZZ9999")
    ex = await example(store)
    assert ex is None  # the code was all they changed

    await edit(me, trip_id, by_kind[Kind.FLIGHT], confirmation_code="QX7", seat="12A")
    ex = await example(store)
    assert ex is not None and not ex.partial
    assert ex.answer.reservations[0].confirmation_code == "QX7"


async def test_a_partly_grounded_correction_is_marked_partial(store: InputStore, me: str) -> None:
    trip_id, by_kind = await filed(store, me)
    await edit(
        me, trip_id, by_kind[Kind.FLIGHT], confirmation_code="ZZ9999", start_local=datetime(2026, 11, 3, 11)
    )
    ex = await example(store)
    assert ex is not None and ex.partial
    assert ex.answer.reservations[0].confirmation_code == "ABC123"
    assert ex.answer.reservations[0].start_local == "2026-11-03T11:00"


async def test_a_deletion_is_taught(store: InputStore, me: str) -> None:
    trip_id, by_kind = await filed(store, me)
    await db.remove_reservation(me, trip_id, by_kind[Kind.LODGING].id)
    ex = await example(store)
    assert ex is not None
    assert [r.kind for r in ex.answer.reservations] == [Kind.FLIGHT]
    assert ex.changes == ["deleted Hotel Avenida"]


async def test_an_example_is_used_only_once_settled_and_only_for_its_owner(
    store: InputStore, me: str
) -> None:
    trip_id, by_kind = await filed(store, me)
    await edit(me, trip_id, by_kind[Kind.FLIGHT], seat="12A")
    ex = await example(store)
    assert ex is not None

    await db.save_example(ex, examples.SETTLE)
    assert await db.live_examples(me) == []
    await db.save_example(ex, timedelta(0))
    assert [e.ingest_id for e in await db.live_examples(me)] == ["mail-1"]
    assert await db.live_examples(await db.user_for_email("someone@else.com")) == []

    await db.delete_example("mail-1")
    assert await db.live_examples(me) == []


async def test_refresh_does_nothing_where_originals_arent_kept(me: str) -> None:
    assert not await examples.refresh("mail-1")
