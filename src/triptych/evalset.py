"""The extraction eval set: plaintext copies of the originals of accounts that opted in to sharing
them (account page → Eval set), in a private bucket the local eval harness reads from.

Sharing copies every original the account already has, then each new one as it arrives
(services/eval_share.py). Each ingest becomes one case, named by its ingest id so it lines up with
inbox_item, extraction_run and the hand-checked labels kept in the private evals repo:

    inputs/<ingest id>/original.<ext>   the bytes as received, decrypted from the inputs bucket
    inputs/<ingest id>/parsed.txt       the text the model was given
    inputs/<ingest id>/case.json        what it was and the run that filed it, the draft to label;
                                        written last, so a case without it is incomplete

case.json also carries the owner's corrections: what they edited on or deleted from the trip page
of the reservations that run made. Editing or deleting one copies its case again (services/user.py),
so `python -m evals sync` can draft the label from the owner's answer rather than the model's.
"""

import asyncio
import base64
import json
import mimetypes
from typing import Any, Self

from triptych import corrections, db, parsing
from triptych.config import Settings, settings
from triptych.inputs import InputStore
from triptych.models import IngestRequest

PREFIX = "inputs"

EXTENSIONS = {"message/rfc822": ".eml", "text/plain": ".txt", "text/html": ".html"}


def extension(content_type: str) -> str:
    return EXTENSIONS.get(content_type) or mimetypes.guess_extension(content_type) or ".bin"


class EvalStore:
    """Where shared cases go. Reads originals through the InputStore, and writes with its client."""

    def __init__(self, inputs: InputStore, bucket: str) -> None:
        self.inputs = inputs
        self.bucket = bucket

    @classmethod
    def from_settings(cls, s: Settings) -> Self | None:
        """None when there's no eval bucket, or no originals to copy into it."""
        if not s.evals_bucket:
            return None
        inputs = InputStore.from_settings(s)
        return cls(inputs, s.evals_bucket) if inputs else None

    async def _put(self, key: str, body: bytes, content_type: str) -> None:
        await asyncio.to_thread(
            self.inputs.s3.put_object, Bucket=self.bucket, Key=key, Body=body, ContentType=content_type
        )

    async def copy(self, ingest_id: str) -> bool:
        """Copy one ingest into the eval set, again if it's already there. False when it isn't
        shared (its owner opted out, or it has no kept original)."""
        case = await db.eval_case(ingest_id)
        if case is None:
            return False
        kept = case.input
        data = await self.inputs.load(kept)
        doc = parsing.parse(
            IngestRequest(
                user=kept.owner,
                source=kept.source,
                filename=kept.filename,
                content_type=kept.content_type,
                data_b64=base64.b64encode(data).decode(),
                received_at=kept.received_at,
            )
        )
        prefix = f"{PREFIX}/{ingest_id}/"
        original = f"original{extension(kept.content_type)}"
        await self._put(prefix + original, data, kept.content_type)
        await self._put(prefix + "parsed.txt", doc.text.encode(), "text/plain; charset=utf-8")
        meta: dict[str, Any] = {
            "ingest_id": ingest_id,
            "owner": kept.owner,
            "label": case.label,
            "state": case.state,
            "source": kept.source,
            "content_type": kept.content_type,
            "filename": kept.filename,
            "received_at": kept.received_at.isoformat(),
            "sha256": kept.sha256.hex(),
            "original": original,
            "subject": doc.subject,
            "images": len(doc.images_b64),
            "run": case.run.model_dump(mode="json", exclude={"ingest_id", "owner", "input_id"})
            if case.run
            else None,
            "corrections": [
                c.model_dump(mode="json")
                for c in corrections.find(
                    ingest_id,
                    case.run.output if case.run else None,
                    case.reservations,
                    case.keys,
                    case.dismissals,
                )
            ],
        }
        await self._put(prefix + "case.json", json.dumps(meta, indent=2).encode(), "application/json")
        await db.mark_eval_copied(ingest_id, kept.owner)
        return True


async def copy(ingest_id: str) -> bool:
    """One case into the eval set, inside `ctx.run` (False when there's nothing to copy)."""
    store = EvalStore.from_settings(settings)
    return await store.copy(ingest_id) if store else False


async def uncopied(owner: str) -> list[str]:
    """What sharing still has to copy for this user, inside `ctx.run` (nothing without a bucket)."""
    return await db.uncopied_eval_ingests(owner) if settings.evals_bucket and settings.inputs_bucket else []
