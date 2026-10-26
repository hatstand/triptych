"""Worked examples for extraction: the owner's corrections on the trip page, kept so that the next
email like it is read the way they would have wanted.

Correcting a reservation (editing, deleting, or a reprocess that changes the run) refreshes its
ingest's example (services/examples.py): the run's answer with the owner's corrections made to it
(corrections.py), next to the text the model was given. Nothing reads them yet; extraction will
show a user's own examples to the model for their later mail, and only theirs.

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

import email
import email.policy
import re
from datetime import timedelta
from email.utils import parseaddr

from triptych import corrections, db
from triptych.config import settings
from triptych.corrections import Correction
from triptych.inputs import InputStore, parse
from triptych.models import RawInput

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


def sender_domain(kept: RawInput, data: bytes) -> str | None:
    """Who sent an emailed original ('iberia.com'): the examples to show first for their next one."""
    if kept.content_type != "message/rfc822":
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
        sender_domain=sender_domain(case.input, data),
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
    await db.save_example(example, SETTLE)
    return True
