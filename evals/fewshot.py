"""The worked examples a case's owner had when it arrived, for `python -m evals run --examples`.

Production shows a new document up to three of its owner's corrected documents (triptych/examples.py).
A case is shown the same, chosen the same way (examples.pick), from the cases on disk that production
would have had an example for: those tagged `corrected` (their owner corrected them in the app) that
the same owner received earlier, never the case itself. The owner is case.json's (the eval bucket
copies it), so local cases, which have none, are shown nothing. An example's answer is its label,
where production's is the run's answer with the owner's corrections: which is what a `corrected`
draft starts out as, and better once verified.
"""

import base64
import json
from dataclasses import dataclass
from pathlib import Path

from evals import score
from evals.cases import Case, Expected, load_cases
from triptych import db, examples, parsing
from triptych.models import Extraction, ExtractionExample, IngestRequest, ParsedDocument


def document(case: Case) -> tuple[ParsedDocument, bytes]:
    """The case's input as production parses it, and its bytes."""
    data = case.input.read_bytes()
    doc = parsing.parse(
        IngestRequest(
            user="eval", source=case.expected.source, filename=case.input.name,
            content_type=case.content_type, data_b64=base64.b64encode(data).decode(),
            received_at=case.expected.received_at,
        )
    )  # fmt: skip
    return doc, data


def owner_of(root: Path, case_id: str) -> str | None:
    try:
        return json.loads((root / "inputs" / case_id / "case.json").read_text()).get("owner")
    except OSError, ValueError:
        return None


def as_answer(expected: Expected) -> Extraction:
    return Extraction(
        is_travel_document=expected.is_travel_document,
        reservations=[score.as_extracted(r) for r in expected.reservations],
    )


@dataclass(frozen=True, slots=True)
class Pool:
    root: Path
    by_owner: dict[str, list[db.LiveExample]]
    """Each owner's possible examples, newest first, as live_examples gives production's."""

    @property
    def size(self) -> int:
        return sum(len(found) for found in self.by_owner.values())

    async def examples_for(self, case: Case, doc: ParsedDocument, data: bytes) -> list[ExtractionExample]:
        owner = owner_of(self.root, case.id)
        earlier = [
            e
            for e in self.by_owner.get(owner or "", [])
            if e.ingest_id != case.id and e.received_at < case.expected.received_at
        ]
        if not earlier:
            return []
        domain = examples.sender_domain(case.content_type, data)
        vector = (
            await examples.embedding(doc.subject, doc.text) if any(e.embedding for e in earlier) else None
        )
        return examples.pick(earlier, domain, doc, room=examples.budget(doc), vector=vector)


async def pool(root: Path) -> Pool:
    """Every case on disk that could be an example, embedded once."""
    by_owner: dict[str, list[db.LiveExample]] = {}
    for case in load_cases(root, include_drafts=True, tags=("corrected",)):
        owner = owner_of(root, case.id)
        doc, data = document(case)
        if owner is None or not doc.text.strip() or len(doc.text) > examples.MAX_TEXT:
            continue
        by_owner.setdefault(owner, []).append(
            db.LiveExample(
                ingest_id=case.id, owner=owner, sender_domain=examples.sender_domain(case.content_type, data),
                subject=doc.subject, text=doc.text, answer=as_answer(case.expected), changes=[],
                partial=False, embedding=await examples.embedding(doc.subject, doc.text),
                received_at=case.expected.received_at,
            )
        )  # fmt: skip
    for found in by_owner.values():
        found.sort(key=lambda e: e.received_at, reverse=True)
    return Pool(root, by_owner)
