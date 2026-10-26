"""Choosing which of a user's worked examples to show the model before a document, and showing them."""

from datetime import UTC, datetime
from typing import Any

import pytest

from triptych import db, examples, llm
from triptych.models import ExtractedReservation, Extraction, Kind, LlmExtractRequest, ParsedDocument

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)
ANSWER = Extraction(
    is_travel_document=True,
    reservations=[ExtractedReservation(kind=Kind.LODGING, title="Casa", start_local="2026-11-03T15:00")],
)


def candidate(ingest_id: str, domain: str | None, text: str) -> db.LiveExample:
    return db.LiveExample(
        ingest_id=ingest_id, owner="me", sender_domain=domain, subject=None, text=text, answer=ANSWER,
        changes=[], partial=False, received_at=NOW,
    )  # fmt: skip


FLIGHT = "Your flight from Madrid to Lisbon departs at nine, booking reference below"
DOC = ParsedDocument(
    subject=None, text="Your flight from Madrid to Porto departs at ten, booking reference below"
)


def test_the_same_senders_come_first_then_alike_ones_with_the_best_nearest_the_document() -> None:
    newest_first = [
        candidate("other-alike", "ryanair.com", FLIGHT),
        candidate("other-unlike", "taberna.pt", "Table for two at eight"),
        candidate("same-unlike", "iberia.com", "Table for two at eight"),
        candidate("same-alike", "iberia.com", FLIGHT),
    ]
    shown = examples.pick(newest_first, "iberia.com", DOC, room=10_000)
    assert [e.ingest_id for e in shown] == ["other-alike", "same-unlike", "same-alike"]


def test_at_most_three_and_only_what_fits() -> None:
    many = [candidate(f"mail-{i}", "iberia.com", FLIGHT) for i in range(5)]
    assert [e.ingest_id for e in examples.pick(many, "iberia.com", DOC, room=10_000)] == [
        "mail-2", "mail-1", "mail-0",
    ]  # fmt: skip
    long = candidate("long", "iberia.com", FLIGHT + " and more" * 1_000)
    assert [e.ingest_id for e in examples.pick([long, *many[:1]], "iberia.com", DOC, room=2_000)] == [
        "mail-0"
    ]


def test_an_upload_is_only_shown_alike_examples() -> None:
    newest_first = [
        candidate("unlike", "iberia.com", "Table for two at eight"),
        candidate("alike", None, FLIGHT),
    ]
    assert [e.ingest_id for e in examples.pick(newest_first, None, DOC, room=10_000)] == ["alike"]


def test_a_long_document_leaves_less_room() -> None:
    assert examples.budget(ParsedDocument(subject=None, text="x" * 30_000)) < examples.budget(DOC)
    assert examples.budget(ParsedDocument(subject=None, text="x" * 40_000)) < 0


@pytest.mark.anyio
async def test_examples_are_shown_as_earlier_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    sent: list[dict[str, Any]] = []

    async def chat(messages: list[dict[str, Any]], **_: Any) -> str:
        sent.extend(messages)
        return Extraction(is_travel_document=False).model_dump_json()

    monkeypatch.setattr(llm, "_chat", chat)
    [shown] = examples.pick([candidate("mail-1", "iberia.com", FLIGHT)], "iberia.com", DOC, room=10_000)
    await llm.extract(LlmExtractRequest(document=DOC, received_at=NOW, examples=[shown]))
    assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"]
    assert FLIGHT in sent[1]["content"] and "Document received: 2026-10-26" in sent[1]["content"]
    assert Extraction.model_validate_json(sent[2]["content"]) == ANSWER
    assert DOC.text in sent[3]["content"]
