"""Worked examples for extraction: the owner's corrections on the trip page, kept so that the next
email like it is read the way they would have wanted.

Correcting a reservation (editing, deleting, or a reprocess that changes the run) refreshes its
ingest's example (services/examples.py): the run's answer with the owner's corrections made to it
(corrections.py), next to the text the model was given. With LLM_EXAMPLES on, extraction shows the
model up to MAX_SHOWN of a user's own examples before each new document of theirs, and never
anyone else's: those from the same sender first, then any that read much like it (EMBED_MODEL's
vectors, or their words where there's no vector).

What a correction may teach is limited, since one bad edit would otherwise be repeated on every
later email from that sender:

- a booking code or service number the owner set is only taught if the text contains it (ignoring
  case, spaces and punctuation), and is otherwise left as the model had it, marking the example
  partial;
- a title or notes they rewrote is how they like their trip shown, not what the email says, so
  it isn't taught;
- an example is usable only SETTLE after its latest correction, so one still being made isn't
  taught half done.
"""

import base64
import email
import email.policy
import logging
import re
from datetime import timedelta
from email.utils import parseaddr

import httpx
import numpy as np

from triptych import corrections, db, llm
from triptych.config import settings
from triptych.corrections import Correction
from triptych.inputs import InputStore, parse
from triptych.models import ExtractionExample, IngestRequest, LlmExtractRequest, ParsedDocument

log = logging.getLogger("triptych.examples")

SETTLE = timedelta(minutes=10)
MAX_TEXT = 12_000
"""Characters (about 3,000 tokens). A longer document makes too long an example to show."""
UNTAUGHT = frozenset({"title", "notes"})
GROUNDED = ("confirmation_code", "service_number")


def _compact(text: str) -> str:
    return re.sub(r"[\W_]+", "", text).casefold()


def grounded(fixes: list[Correction], text: str) -> tuple[list[Correction], bool]:
    """The corrections an example may teach, and whether any were left out for not being in
    `text`. A reservation the run didn't find keeps its title, which it can't do without."""
    seen = _compact(text)
    out, partial = [], False
    for c in fixes:
        fields = {k: v for k, v in c.fields.items() if c.reservation is None or k not in UNTAUGHT}
        for f in GROUNDED:
            if (value := fields.get(f)) and _compact(str(value)) not in seen:
                del fields[f]
                partial = True
        if c.deleted or c.reservation is None or fields:
            out.append(
                c.model_copy(update={"fields": fields, "edited": [f for f in c.edited if f in fields]})
            )
    return out, partial


def sender_domain(content_type: str, data: bytes) -> str | None:
    """Who sent an emailed original ('iberia.com'): the examples to show first for their next one."""
    if content_type != "message/rfc822":
        return None
    sender = email.message_from_bytes(data, policy=email.policy.default).get("From", "")
    _, address = parseaddr(str(sender))
    return address.rpartition("@")[2].lower() or None


def build(case: db.EvalCase, data: bytes) -> db.Example | None:
    """The ingest's example, or None when its owner corrected nothing it may teach."""
    output = case.run.output if case.run else None
    fixes = corrections.find(case.ingest_id, output, case.reservations, case.keys, case.dismissals)
    if not fixes:
        return None
    doc = parse(case.input, data)
    if not doc.text.strip() or len(doc.text) > MAX_TEXT:
        return None
    fixes, partial = grounded(fixes, doc.text)
    if not fixes:
        return None
    answer, changes = corrections.apply(output, fixes)
    return db.Example(
        ingest_id=case.ingest_id,
        owner=case.input.owner,
        sender_domain=sender_domain(case.input.content_type, data),
        subject=doc.subject,
        text=doc.text,
        answer=answer,
        changes=changes,
        partial=partial,
    )


async def refresh(ingest_id: str) -> bool:
    """Rebuild an ingest's example from its owner's corrections, inside `ctx.run`. False when it
    has none (any old one is removed) or originals aren't kept here."""
    store = InputStore.from_settings(settings)
    case = await db.ingest_case(ingest_id)
    if store is None or case is None:
        return False
    example = build(case, await store.load(case.input))
    if example is None:
        await db.delete_example(ingest_id)
        return False
    example.embedding = await embedding(example.subject, example.text)
    await db.save_example(example, SETTLE)
    return True


MAX_SHOWN = 3
SIMILAR = 0.3
"""How alike in words (Jaccard) an example from another sender must be to the document to be shown,
when either has no vector."""
SIMILAR_MEANING = 0.6
"""How alike in meaning (cosine of their vectors) they must be otherwise."""
CHARS_PER_TOKEN = 3
"""A cautious guess for mixed prose and JSON, so the examples never crowd the document out."""
PROMPT_TOKENS = 1_500
"""The system prompt and the turn framing."""
IMAGE_TOKENS = 300


def _words(subject: str | None, text: str) -> set[str]:
    return set(re.findall(r"[^\W\d_]{3,}", f"{subject or ''} {text}".casefold()))


def _alike(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a and b else 0.0


def _cosine(a: list[float], b: list[float]) -> float:
    u, v = np.asarray(a), np.asarray(b)
    return float(u @ v / (np.linalg.norm(u) * np.linalg.norm(v) or 1.0))


async def embedding(subject: str | None, text: str) -> list[float] | None:
    """EMBED_MODEL's vector for a document, or None if Ollama couldn't give one: an example or a
    document without one is compared by its words instead."""
    try:
        [vector] = await llm.embed([f"{subject or ''}\n{text}"])
    except (httpx.HTTPError, llm.LlmError) as e:
        log.warning("no embedding: %s", e)
        return None
    return vector


def budget(doc: ParsedDocument) -> int:
    """The characters the examples may take up alongside `doc` in the context window."""
    tokens = settings.llm_num_ctx - llm.MAX_OUTPUT_TOKENS - PROMPT_TOKENS - IMAGE_TOKENS * len(doc.images_b64)
    return tokens * CHARS_PER_TOKEN - len(doc.text) - len(doc.subject or "")


def pick(
    candidates: list[db.LiveExample],
    domain: str | None,
    doc: ParsedDocument,
    *,
    room: int,
    vector: list[float] | None = None,
) -> list[ExtractionExample]:
    """Up to MAX_SHOWN of `candidates` (newest first) to show before `doc`, whose vector is
    `vector`, in at most `room` characters. The same sender's come before other senders', and the
    most alike before the rest (a cosine is usually well above a Jaccard, so those compared by
    meaning tend to come first). They're returned in showing order, so the best one comes last,
    nearest the document."""
    words = _words(doc.subject, doc.text)
    ranked: list[tuple[bool, float, db.LiveExample]] = []
    for ex in candidates:
        same = domain is not None and ex.sender_domain == domain
        if vector and ex.embedding:
            alike = _cosine(vector, ex.embedding)
            enough = alike >= SIMILAR_MEANING
        else:
            alike = _alike(words, _words(ex.subject, ex.text))
            enough = alike >= SIMILAR
        if same or enough:
            ranked.append((not same, -alike, ex))
    chosen: list[ExtractionExample] = []
    for *_, ex in sorted(ranked, key=lambda r: r[:2]):
        cost = len(ex.text) + len(ex.subject or "") + len(ex.answer.model_dump_json())
        if cost > room:
            continue
        room -= cost
        chosen.append(ExtractionExample.model_validate(ex.model_dump()))
        if len(chosen) == MAX_SHOWN:
            break
    return chosen[::-1]


async def with_examples(extract: LlmExtractRequest, ingest_id: str, req: IngestRequest) -> LlmExtractRequest:
    """`extract` (the new ingest's extraction) with its owner's examples for it, inside `ctx.run`."""
    doc = extract.document
    candidates = [e for e in await db.live_examples(req.user) if e.ingest_id != ingest_id]
    if not candidates:
        return extract
    domain = sender_domain(req.content_type, base64.b64decode(req.data_b64))
    vector = await embedding(doc.subject, doc.text) if any(e.embedding for e in candidates) else None
    return extract.model_copy(
        update={"examples": pick(candidates, domain, doc, room=budget(doc), vector=vector)}
    )
