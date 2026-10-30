"""Sharing with the extraction eval set: opted-in accounts' originals copied, decrypted, to the eval
bucket (moto stands in for R2), and the account page that turns it on and off."""

import dataclasses
import json
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import httpx
import pytest
from moto import mock_aws

from triptych import db, enrich, evalset, inputs
from triptych.auth import Me
from triptych.config import settings
from triptych.evalset import EvalStore
from triptych.inputs import InputStore
from triptych.models import (
    ExtractedReservation,
    Extraction,
    ExtractionRun,
    InboxItem,
    Kind,
    ReservationFields,
)
from triptych.web import app as web
from triptych.web import deps, ratelimit

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

INPUTS = "triptych-inputs"
EVALS = "triptych-evals"
NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
EMAIL = b"From: airline@example.com\r\nSubject: Your flight\r\n\r\nLHR to LIS, 3 Nov, BA500.\r\n"
ORIGIN = settings.allowed_origins[0]


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=INPUTS)
        client.create_bucket(Bucket=EVALS)
        yield client


@pytest.fixture
def store(s3: Any) -> EvalStore:
    s = dataclasses.replace(
        settings,
        inputs_bucket=INPUTS,
        inputs_master_keyset=inputs.new_master_keyset(),
        inputs_access_key_id="test",
        inputs_secret_access_key="test",
        evals_bucket=EVALS,
    )
    st = EvalStore.from_settings(s)
    assert st is not None
    st.inputs.s3 = s3
    return st


@pytest.fixture
async def me(database: str) -> str:
    return await db.user_for_email("me@example.com")


async def ingest(
    store: InputStore, owner: str, ingest_id: str, data: bytes = EMAIL, output: Extraction | None = None
) -> None:
    """An ingest as Ingest leaves it: in the inbox, original kept, run recorded."""
    await db.add_inbox_item(
        owner,
        InboxItem(ingest_id=ingest_id, source="email", label="Your flight", received_at=NOW, state="done"),
    )
    kept = await store.keep(
        owner, data, ingest_id=ingest_id, source="email", content_type="message/rfc822", filename=None,
        received_at=NOW,
    )  # fmt: skip
    await db.add_extraction_run(
        ExtractionRun(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, ingest_id)),
            ingest_id=ingest_id,
            owner=owner,
            input_id=kept.id,
            model="gemma4:26b-a4b-it-qat",
            model_digest=None,
            prompt_version="0123456789ab",
            code_version="dev",
            params={"think": False},
            purpose="ingest",
            output=output or Extraction(is_travel_document=False, reservations=[]),
        )
    )


def keys(s3: Any) -> set[str]:
    return {o["Key"] for o in s3.list_objects_v2(Bucket=EVALS).get("Contents", [])}


async def test_shared_ingest_is_copied_in_plaintext_with_its_run(store: EvalStore, s3: Any, me: str) -> None:
    await ingest(store.inputs, me, "mail-1")
    await db.set_eval_sharing(me, True)

    assert await store.copy("mail-1")

    assert keys(s3) == {"inputs/mail-1/original.eml", "inputs/mail-1/parsed.txt", "inputs/mail-1/case.json"}
    assert s3.get_object(Bucket=EVALS, Key="inputs/mail-1/original.eml")["Body"].read() == EMAIL
    assert b"BA500" in s3.get_object(Bucket=EVALS, Key="inputs/mail-1/parsed.txt")["Body"].read()
    case = json.loads(s3.get_object(Bucket=EVALS, Key="inputs/mail-1/case.json")["Body"].read())
    assert case["ingest_id"] == "mail-1" and case["subject"] == "Your flight"
    assert case["run"]["model"] == "gemma4:26b-a4b-it-qat"
    assert case["run"]["output"] == {"is_travel_document": False, "reservations": []}
    assert await db.uncopied_eval_ingests(me) == []


TRIP = Extraction(
    is_travel_document=True,
    reservations=[
        ExtractedReservation(
            kind=Kind.FLIGHT, title="BA 500 LHR→LIS", confirmation_code="ABC123", service_number="BA 500",
            start_local="2026-11-03T09:00", end_local="2026-11-03T11:45", start_iata="LHR", end_iata="LIS",
            city="Lisbon", country_code="PT",
        ),
        ExtractedReservation(
            kind=Kind.LODGING, title="Hotel Avenida", confirmation_code="H1", start_local="2026-11-03T15:00",
            end_local="2026-11-06T11:00", city="Lisbon", country_code="PT",
        ),
        ExtractedReservation(kind=Kind.OTHER, title="Unparseable", start_local="sometime"),
    ],
)  # fmt: skip


async def test_corrections_on_the_trip_page_are_copied_as_the_owners_answer(
    store: EvalStore, s3: Any, me: str
) -> None:
    await ingest(store.inputs, me, "mail-1", output=TRIP)
    await db.set_eval_sharing(me, True)
    made = [
        r
        for x in TRIP.reservations
        if (r := enrich.normalise(x, reservation_id=str(uuid.uuid4()), ingest_id="mail-1")) is not None
    ]
    [trip_id] = (await db.file_reservations(me, "mail-1", made)).trip_ids
    flight, hotel = made

    def case() -> dict[str, Any]:
        return json.loads(s3.get_object(Bucket=EVALS, Key="inputs/mail-1/case.json")["Body"].read())

    assert await store.copy("mail-1")
    assert case()["corrections"] == []

    # The flight left later than the email said, from another seat, and the hotel was cancelled.
    fix = ReservationFields.model_validate(
        flight.model_dump() | {"start_local": datetime(2026, 11, 3, 10, 15), "seat": "12A"}
    )
    await db.edit_reservation(me, trip_id, flight.id, fix)
    removed = await db.remove_reservation(me, trip_id, hotel.id)
    assert removed.ingest_id == "mail-1"

    assert await store.copy("mail-1")
    assert case()["corrections"] == [
        {"reservation": 0, "deleted": False, "edited": ["seat", "start_local"],
         "fields": {"seat": "12A", "start_local": "2026-11-03T10:15:00"}},
        {"reservation": 1, "deleted": True, "edited": [], "fields": {}},
    ]  # fmt: skip


async def test_nothing_is_copied_unless_the_owner_shares(store: EvalStore, s3: Any, me: str) -> None:
    await ingest(store.inputs, me, "mail-1")
    assert not await store.copy("mail-1")

    await db.set_eval_sharing(me, True)
    await db.set_eval_sharing(me, False)
    assert not await store.copy("mail-1")
    assert keys(s3) == set()


async def test_sharing_lists_only_what_isnt_copied_yet(store: EvalStore, me: str) -> None:
    other = await db.user_for_email("you@example.com")
    await ingest(store.inputs, me, "mail-1")
    await ingest(store.inputs, me, "mail-2", b"another booking")
    await ingest(store.inputs, other, "mail-3")
    await db.set_eval_sharing(me, True)
    await db.set_eval_sharing(other, True)
    await db.set_eval_sharing(other, False)

    assert await db.uncopied_eval_ingests(me) == ["mail-1", "mail-2"]
    assert await db.uncopied_eval_ingests(other) == []
    await store.copy("mail-1")
    assert await db.uncopied_eval_ingests(me) == ["mail-2"]
    sharing = await db.eval_sharing(me)
    assert sharing is not None and (sharing.copied, sharing.total) == (1, 2)
    assert await db.eval_sharing(other) is None


async def test_unconfigured_eval_set_copies_nothing(me: str) -> None:
    assert settings.evals_bucket is None
    assert await evalset.copy("mail-1") is False
    await db.set_eval_sharing(me, True)
    assert await evalset.uncopied(me) == []


class FakeRestate:
    def __init__(self) -> None:
        self.sent: list[tuple[object, str]] = []

    async def object_send(self, handler: object, key: str, _arg: None) -> None:
        self.sent.append((handler, key))


@pytest.fixture
async def browser(
    me: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[tuple[httpx.AsyncClient, FakeRestate]]:
    monkeypatch.setattr(
        web, "settings", dataclasses.replace(settings, inputs_bucket=INPUTS, evals_bucket=EVALS)
    )
    fake = FakeRestate()
    web.app.dependency_overrides[deps.current_user] = lambda: Me(id=me, email="me@example.com")
    web.app.dependency_overrides[deps.rs] = lambda: fake
    web.app.dependency_overrides[deps.limiter] = ratelimit.limiter
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        yield c, fake
    web.app.dependency_overrides.clear()


async def test_opting_in_shares_everything_and_opting_out_stops(
    browser: tuple[httpx.AsyncClient, FakeRestate], me: str
) -> None:
    client, fake = browser
    page = await client.get("/account/sharing")
    assert page.status_code == 200 and "Share my bookings" in page.text

    assert (await client.post("/account/sharing", data={"share": "true"})).status_code == 303
    assert await db.eval_sharing(me) is not None
    assert fake.sent == [(web.eval_share_svc.share_all, me)]
    assert "Sharing since" in (await client.get("/account/sharing")).text

    assert (await client.post("/account/sharing", data={"share": "false"})).status_code == 303
    assert await db.eval_sharing(me) is None
    assert len(fake.sent) == 1


async def test_sharing_isnt_offered_without_an_eval_bucket(me: str) -> None:
    web.app.dependency_overrides[deps.current_user] = lambda: Me(id=me, email="me@example.com")
    web.app.dependency_overrides[deps.rs] = FakeRestate
    transport = httpx.ASGITransport(app=web.app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
            assert "Eval set" not in (await c.get("/account")).text
            assert (await c.get("/account/sharing")).status_code == 404
            assert (await c.post("/account/sharing", data={"share": "true"})).status_code == 404
    finally:
        web.app.dependency_overrides.clear()
    assert await db.eval_sharing(me) is None
