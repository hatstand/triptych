"""Keeping originals: encrypted per user in an S3 bucket (moto stands in for R2), recorded in Postgres."""

import base64
import dataclasses
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import boto3
import pytest
from moto import mock_aws

from triptych import db, inputs
from triptych.config import settings
from triptych.inputs import InputStore, IntegrityError
from triptych.models import Extraction, ExtractionRun, InboxItem, IngestRequest

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

BUCKET = "triptych-inputs"
NOW = datetime(2026, 10, 24, 12, 0, tzinfo=UTC)
EMAIL = b"From: airline@example.com\r\nSubject: Your flight\r\n\r\nLHR to LIS, 3 Nov, BA500.\r\n"


@pytest.fixture
def s3() -> Iterator[Any]:
    with mock_aws():
        client = boto3.client("s3", region_name="us-east-1")
        client.create_bucket(Bucket=BUCKET)
        yield client


@pytest.fixture
def store(s3: Any) -> InputStore:
    s = dataclasses.replace(
        settings,
        inputs_bucket=BUCKET,
        inputs_master_keyset=inputs.new_master_keyset(),
        inputs_endpoint_url=None,
        inputs_access_key_id="test",
        inputs_secret_access_key="test",
    )
    st = InputStore.from_settings(s)
    assert st is not None
    st.s3 = s3
    return st


@pytest.fixture
async def me(database: str) -> str:
    return await db.user_for_email("me@example.com")


async def inbox(owner: str, ingest_id: str) -> None:
    await db.add_inbox_item(
        owner, InboxItem(ingest_id=ingest_id, source="email", label="x", received_at=NOW, state="queued")
    )


async def keep(store: InputStore, owner: str, data: bytes, ingest_id: str = "mail-1"):
    return await store.keep(
        owner, data, ingest_id=ingest_id, source="email", content_type="message/rfc822", filename=None,
        received_at=NOW,
    )  # fmt: skip


async def test_round_trip_and_bucket_holds_only_ciphertext(store: InputStore, s3: Any, me: str) -> None:
    await inbox(me, "mail-1")
    kept = await keep(store, me, EMAIL)

    assert kept.object_key.startswith(f"v1/{me}/")
    stored = s3.get_object(Bucket=BUCKET, Key=kept.object_key)["Body"].read()
    assert b"BA500" not in stored
    assert await store.load(kept) == EMAIL
    assert await db.raw_input(kept.id) == kept
    async with db.connection() as conn:
        cur = await conn.execute("SELECT input_id::text FROM inbox_item WHERE ingest_id = 'mail-1'")
        assert await cur.fetchone() == (kept.id,)


async def test_same_bytes_are_kept_once_per_user(store: InputStore, s3: Any, me: str) -> None:
    other = await db.user_for_email("you@example.com")
    a = await keep(store, me, EMAIL, "mail-1")
    b = await keep(store, me, EMAIL, "mail-2")
    c = await keep(store, other, EMAIL, "mail-3")

    assert a.id == b.id and a.object_key == b.object_key
    # Another user's copy is theirs: its own row, name and key.
    assert c.id != a.id and c.object_key.split("/")[2] != a.object_key.split("/")[2]
    assert len(s3.list_objects_v2(Bucket=BUCKET)["Contents"]) == 2
    assert await store.load(c) == EMAIL


async def test_swapped_object_is_refused(store: InputStore, s3: Any, me: str) -> None:
    a = await keep(store, me, EMAIL, "mail-1")
    b = await keep(store, me, b"another booking", "mail-2")
    s3.copy_object(Bucket=BUCKET, Key=a.object_key, CopySource={"Bucket": BUCKET, "Key": b.object_key})
    with pytest.raises(IntegrityError):
        await store.load(a)


async def test_deleting_the_user_shreds_their_keys(store: InputStore, me: str) -> None:
    await keep(store, me, EMAIL)
    async with db.connection() as conn:
        await conn.execute("DELETE FROM app_user WHERE id = %s", (me,))
    assert await db.user_keysets(me) is None


async def test_unconfigured_store_keeps_nothing(me: str) -> None:
    req = IngestRequest(user=me, source="paste", data_b64=base64.b64encode(b"hi").decode(), received_at=NOW)
    assert settings.inputs_bucket is None
    assert await inputs.keep("web-1", req) is None


async def test_extraction_run_becomes_the_ingest_current_run(store: InputStore, me: str) -> None:
    await inbox(me, "mail-1")
    kept = await keep(store, me, EMAIL)
    run = ExtractionRun(
        id="00000000-0000-0000-0000-000000000001",
        ingest_id="mail-1",
        owner=me,
        input_id=kept.id,
        model="gemma4:31b",
        model_digest="sha256:abc",
        prompt_version="0123456789ab",
        code_version="dev",
        params={"think": False},
        purpose="ingest",
        output=Extraction(is_travel_document=False, reservations=[]),
    )
    await db.add_extraction_run(run)
    await db.add_extraction_run(run)  # a retried step
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT r.output, i.current_run_id::text FROM extraction_run r
               JOIN inbox_item i ON i.ingest_id = r.ingest_id"""
        )
        assert await cur.fetchall() == [({"is_travel_document": False, "reservations": []}, run.id)]
