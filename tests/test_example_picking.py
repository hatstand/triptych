"""Choosing which of a user's worked examples to show the model before a document, and showing them."""

import functools
import json
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from triptych import db, examples, llm
from triptych.config import settings
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


def with_vector(ex: db.LiveExample, vector: list[float]) -> db.LiveExample:
    return ex.model_copy(update={"embedding": vector})


def test_examples_with_vectors_are_compared_by_meaning() -> None:
    newest_first = [
        with_vector(candidate("near", "ryanair.com", "Table for two at eight"), [0.9, 0.1]),
        with_vector(candidate("far", "taberna.pt", FLIGHT), [0.0, 1.0]),
        candidate("no-vector", "renfe.com", FLIGHT),
    ]
    shown = examples.pick(newest_first, None, DOC, room=10_000, vector=[1.0, 0.0])
    # "far" shares DOC's words but not its meaning; "no-vector" is compared by its words
    assert [e.ingest_id for e in shown] == ["no-vector", "near"]
    without = examples.pick(newest_first, None, DOC, room=10_000)  # no vector for the document
    assert [e.ingest_id for e in without] == ["no-vector", "far"]  # so words for all


@pytest.mark.anyio
async def test_an_embedding_asks_for_similarity_and_is_none_when_ollama_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent: list[dict[str, Any]] = []

    def handle(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        if len(sent) > 1:
            return httpx.Response(404, text="model not found")
        return httpx.Response(200, json={"embeddings": [[0.6, 0.8]]})

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(client, transport=httpx.MockTransport(handle))
    )
    assert await examples.embedding("Your flight", "Madrid to Lisbon") == [0.6, 0.8]
    assert sent[0]["model"] == settings.embed_model
    assert sent[0]["input"] == ["task: sentence similarity | query: Your flight\nMadrid to Lisbon"]
    assert await examples.embedding(None, "Madrid to Lisbon") is None
