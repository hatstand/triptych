"""Originals: every forwarded email, upload and paste, kept so it can be re-extracted later with a
new model or prompt, and so the data derived from it can be rebuilt.

The bytes go to an S3-compatible bucket (Cloudflare R2 in production), encrypted here, before they
leave the machine, with the owner's own Tink keysets, so the bucket only ever holds ciphertext:

    v1/<user id>/<PRF of the bytes>   holds   AEAD(bytes, associated data = that object key)

Each user has an AEAD keyset, which encrypts, and a PRF keyset, which names objects. The same bytes
from the same user always get the same name, so they are stored once, but nobody without the key
can check whether some known file is stored. Originals are never deduped across users.

Both keysets are kept in Postgres (user_key), encrypted by the master keyset (INPUTS_MASTER_KEYSET),
which exists only on marvin and, as a copy, off the box. Deleting a user's row makes every copy of
their originals unreadable, backups included.

    uv run python -m triptych.inputs new-master-keyset   # prints a new master keyset
"""

import asyncio
import base64
import hashlib
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Self

import boto3
import restate
import tink
from botocore.config import Config
from tink import aead, prf, secret_key_access

from triptych import db, parsing
from triptych.config import Settings, settings
from triptych.models import IngestRequest, IngestSource, ParsedDocument, RawInput

LAYOUT = "v1"


class IntegrityError(RuntimeError):
    """An object didn't decrypt, or decrypted to bytes other than the ones recorded."""


@dataclass(frozen=True, slots=True)
class UserKeys:
    aead: aead.Aead
    prf: prf.Prf


def _keyset_ad(owner: str, kind: str) -> bytes:
    """Ties an encrypted keyset to its user and purpose, so rows can't be swapped."""
    return f"triptych user_key {owner} {kind}".encode()


def new_master_keyset() -> str:
    aead.register()
    handle = tink.new_keyset_handle(aead.aead_key_templates.AES256_GCM)
    # On one line, so it fits in .env.
    return json.dumps(json.loads(tink.json_proto_keyset_format.serialize(handle, secret_key_access.TOKEN)))


class InputStore:
    """Where originals are kept. Made once per use from settings (`from_settings`)."""

    def __init__(self, s3: Any, bucket: str, master: aead.Aead) -> None:
        self.s3 = s3
        self.bucket = bucket
        self.master = master

    @classmethod
    def from_settings(cls, s: Settings) -> Self | None:
        """None when no bucket is configured (development), so originals aren't kept."""
        if not s.inputs_bucket:
            return None
        if not s.inputs_master_keyset:
            raise RuntimeError("INPUTS_BUCKET is set but INPUTS_MASTER_KEYSET isn't")
        aead.register()
        prf.register()
        master = tink.json_proto_keyset_format.parse(
            s.inputs_master_keyset, secret_key_access.TOKEN
        ).primitive(aead.Aead)
        s3 = boto3.client(
            "s3",
            endpoint_url=s.inputs_endpoint_url,
            aws_access_key_id=s.inputs_access_key_id,
            aws_secret_access_key=s.inputs_secret_access_key,
            region_name="auto",
            # R2 doesn't take the checksums newer botocore sends by default.
            config=Config(
                request_checksum_calculation="when_required", response_checksum_validation="when_required"
            ),
        )
        return cls(s3, s.inputs_bucket, master)

    async def keys(self, owner: str) -> UserKeys:
        """The user's keysets, made on first use."""
        stored = await db.user_keysets(owner)
        if stored is None:
            a = tink.new_keyset_handle(aead.aead_key_templates.AES256_GCM)
            p = tink.new_keyset_handle(prf.prf_key_templates.HMAC_SHA256)
            stored = await db.add_user_keysets(
                owner,
                tink.proto_keyset_format.serialize_encrypted(a, self.master, _keyset_ad(owner, "aead")),
                tink.proto_keyset_format.serialize_encrypted(p, self.master, _keyset_ad(owner, "prf")),
            )
        a_blob, p_blob = stored
        a = tink.proto_keyset_format.parse_encrypted(a_blob, self.master, _keyset_ad(owner, "aead"))
        p = tink.proto_keyset_format.parse_encrypted(p_blob, self.master, _keyset_ad(owner, "prf"))
        return UserKeys(aead=a.primitive(aead.Aead), prf=p.primitive(prf.PrfSet).primary())

    async def keep(
        self,
        owner: str,
        data: bytes,
        *,
        ingest_id: str,
        source: IngestSource,
        content_type: str,
        filename: str | None,
        received_at: datetime,
    ) -> RawInput:
        """Store `data` (again, harmlessly, if it's already there) and record it against the ingest."""
        keys = await self.keys(owner)
        sha256 = hashlib.sha256(data).digest()
        object_key = f"{LAYOUT}/{owner}/{keys.prf.compute(data, 32).hex()}"
        ciphertext = keys.aead.encrypt(data, object_key.encode())
        await asyncio.to_thread(self.s3.put_object, Bucket=self.bucket, Key=object_key, Body=ciphertext)
        kept = RawInput(
            id=str(uuid.uuid5(uuid.NAMESPACE_URL, f"triptych:input:{owner}/{sha256.hex()}")),
            owner=owner,
            sha256=sha256,
            object_key=object_key,
            size=len(data),
            source=source,
            content_type=content_type,
            filename=filename,
            received_at=received_at,
        )
        await db.add_raw_input(kept, ingest_id)
        return kept

    async def load(self, kept: RawInput) -> bytes:
        """The original bytes, checked against what was recorded when they were kept."""
        keys = await self.keys(kept.owner)
        resp = await asyncio.to_thread(self.s3.get_object, Bucket=self.bucket, Key=kept.object_key)
        ciphertext = await asyncio.to_thread(resp["Body"].read)
        try:
            data = keys.aead.decrypt(ciphertext, kept.object_key.encode())
        except tink.TinkError as e:
            raise IntegrityError(f"{kept.object_key} didn't decrypt") from e
        if hashlib.sha256(data).digest() != kept.sha256:
            raise IntegrityError(f"{kept.object_key} isn't the bytes that were kept")
        return data


async def keep(ingest_id: str, req: IngestRequest) -> str | None:
    """The Ingest workflow's first step: keep the original, returning its raw_input id (None when
    originals aren't kept here)."""
    store = InputStore.from_settings(settings)
    if store is None:
        return None
    kept = await store.keep(
        req.user,
        base64.b64decode(req.data_b64),
        ingest_id=ingest_id,
        source=req.source,
        content_type=req.content_type,
        filename=req.filename,
        received_at=req.received_at,
    )
    return kept.id


async def load_original(input_id: str) -> ParsedDocument:
    """The Reprocess workflow's first step: an original, decrypted and parsed as Ingest parsed it."""
    store = InputStore.from_settings(settings)
    if store is None:
        raise restate.TerminalError("originals aren't kept here (INPUTS_BUCKET isn't set)")
    kept = await db.raw_input(input_id)
    if kept is None:
        raise restate.TerminalError(f"no original {input_id}")
    try:
        data = await store.load(kept)
    except IntegrityError as e:
        raise restate.TerminalError(str(e)) from e
    return parsing.parse(
        IngestRequest(
            user=kept.owner,
            source=kept.source,
            filename=kept.filename,
            content_type=kept.content_type,
            data_b64=base64.b64encode(data).decode(),
            received_at=kept.received_at,
        )
    )


if __name__ == "__main__":
    if sys.argv[1:] != ["new-master-keyset"]:
        sys.exit("usage: python -m triptych.inputs new-master-keyset")
    print(new_master_keyset())
