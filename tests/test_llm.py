"""The Ollama calls, against a stand-in server: what's sent, and what comes back when it fails."""

import functools
import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import ollama
import pytest

from triptych import llm
from triptych.config import settings
from triptych.models import Extraction, LlmExtractRequest, ParsedDocument

pytestmark = pytest.mark.anyio

REQ = LlmExtractRequest(
    document=ParsedDocument(subject="Hi", text="Nothing here"), received_at=datetime.now(UTC)
)


def serve(monkeypatch: pytest.MonkeyPatch, handle: Callable[[httpx.Request], httpx.Response]) -> list[Any]:
    sent: list[Any] = []

    def record(request: httpx.Request) -> httpx.Response:
        sent.append((request.url.path, json.loads(request.content or b"null")))
        return handle(request)

    client = httpx.AsyncClient
    monkeypatch.setattr(
        httpx, "AsyncClient", functools.partial(client, transport=httpx.MockTransport(record))
    )
    return sent


def chat_reply(content: str) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "model": settings.llm_model,
            "message": {"role": "assistant", "content": content},
            "done": True,
        },
    )


async def test_extraction_asks_for_the_schema_with_the_recorded_settings(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = serve(monkeypatch, lambda _: chat_reply(Extraction(is_travel_document=False).model_dump_json()))
    assert await llm.extract(REQ) == Extraction(is_travel_document=False)
    [(path, body)] = sent
    assert path == "/api/chat"
    assert body["model"] == settings.llm_model
    assert body["format"] == Extraction.model_json_schema()
    assert (body["think"], body["options"], body["keep_alive"]) == (
        False,
        llm.chat_params()["options"],
        "30m",
    )
    assert [m["role"] for m in body["messages"]] == ["system", "user"]


async def test_an_empty_answer_or_a_missing_model_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    serve(monkeypatch, lambda _: chat_reply("  "))
    with pytest.raises(llm.LlmError, match="empty message"):
        await llm.extract(REQ)
    serve(monkeypatch, lambda _: httpx.Response(404, json={"error": "model not found"}))
    with pytest.raises(ollama.ResponseError) as e:
        await llm.extract(REQ)
    assert isinstance(e.value, llm.ERRORS)


async def test_the_digest_and_health_come_from_the_pulled_models(monkeypatch: pytest.MonkeyPatch) -> None:
    tags = {"models": [{"model": settings.llm_model, "digest": "sha256:abc", "size": 1}]}
    serve(monkeypatch, lambda _: httpx.Response(200, json=tags))
    assert await llm.model_digest(settings.llm_model) == "sha256:abc"
    assert await llm.model_digest("qwen3.6:35b") is None
    assert (await llm.health())["available"]


async def test_an_owners_word_that_its_a_booking_goes_with_the_document(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sent = serve(monkeypatch, lambda _: chat_reply(Extraction(is_travel_document=True).model_dump_json()))
    await llm.extract(REQ)
    await llm.extract(REQ.model_copy(update={"owner_says_travel": True}))
    plain, told = (body["messages"][-1]["content"] for _, body in sent)
    assert llm.OWNER_SAYS_TRAVEL not in plain
    assert told == plain + llm.OWNER_SAYS_TRAVEL
