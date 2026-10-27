"""Extraction and the other model calls, over the `ollama` client to the local server (Gemma 4 by
default).

Errors are raised, never swallowed (ollama.ResponseError, ConnectionError, LlmError): callers run
these inside `ctx.run`, so Restate owns retries.
"""

import hashlib
import json
from collections.abc import Sequence
from datetime import datetime
from typing import Any

import httpx
import ollama

from triptych.config import settings
from triptych.models import (
    Answer,
    Extraction,
    LlmAskRequest,
    LlmExtractRequest,
    LlmSubjectRequest,
    SubjectBox,
)

EXTRACT_SYSTEM = """\
You extract travel reservations from confirmation emails, receipts, boarding passes and screenshots.

Rules:
- Output one reservation per bookable segment. A round-trip flight is TWO flight reservations; a \
connection (SFO→JFK→LIS) is one reservation per leg.
- Hotels, apartments and other stays are kind "lodging": start_local is check-in, end_local is check-out.
- Ferries are kind "ferry": start_location and end_location are the ports, start_iata and end_iata \
are null (ports have no IATA codes), and service_number is the sailing number if one is given.
- Car rentals: start = pickup, end = drop-off. Restaurants, tours, tickets, events: "restaurant"/"activity".
- Times are LOCAL to where they happen, formatted YYYY-MM-DDTHH:MM. If only a date is given use \
15:00 for hotel check-in, 11:00 for check-out, 09:00 otherwise.
- If the year is missing, infer the next occurrence on or after the date the document was received.
- For flights always fill start_iata/end_iata (3-letter airport codes) and service_number (the \
flight number). For trains fill service_number with the train number.
- city is where the traveller will BE (the arrival city for transport, the hotel's city for lodging).
- Always fill country_code for city. For transport also fill start_city (where it departs from, in \
English) and start_country_code: a train from Chengdu East to Beijing West has start_city "Chengdu", \
city "Beijing", both CN.
- Fill start_timezone and end_timezone with the IANA timezones where the reservation starts and \
ends. For transport they are start_city's and city's: a flight from London to Beijing has \
start_timezone "Europe/London" and end_timezone "Asia/Shanghai". A hotel in Beijing has \
Asia/Shanghai for both.
- Never invent confirmation codes, seats or times that are not in the document. Use null instead.
- If the document contains no travel reservations (newsletters, marketing, receipts for unrelated \
things) set is_travel_document=false and return an empty list.
"""

ASK_SYSTEM = """\
You are a concise travel assistant. Facts about bookings (times, codes, addresses) must come ONLY \
from the itinerary below; never invent them. You may add general travel know-how (e.g. arrive ~3 h \
before an international flight, ~2 h for domestic) when it helps, and say that it is general advice. \
Times are local to where each segment happens.
"""


SUBJECT_SYSTEM = """\
You look at a photo that represents a travel destination and find its main subject: the landmark, \
skyline, building or view that makes it recognisably that place (the tower, not the sky around it; \
the whole skyline, not one window). Name the subject, then give the box that just contains it as \
percentages of the image: left and right from the left edge, top and bottom from the top edge (0 to \
100). Include all of a tall subject, from its very top.
"""


class LlmError(RuntimeError):
    pass


ERRORS = (ollama.ResponseError, ConnectionError, httpx.HTTPError, LlmError)
"""What a call here raises when Ollama couldn't answer: an error status, no connection, a timeout
(the client's httpx raises those as they are), or an answer that's no use."""


MAX_OUTPUT_TOKENS = 4096

Think = bool | str
"""Ollama's `think`: on or off, or a level ("low", "medium", "high") for models that take one."""


def chat_params(think: Think = False) -> dict[str, Any]:
    """The generation settings every call uses (recorded with each extraction run)."""
    return {
        # Gemma 4 thinks by default; for extraction that mostly burns tokens (and can loop until
        # the context is exhausted), so it's off unless an eval asks, and output is capped to fail
        # fast and retry instead.
        "think": think,
        "options": {"temperature": 0, "num_ctx": settings.llm_num_ctx, "num_predict": MAX_OUTPUT_TOKENS},
    }


def prompt_version(system: str = EXTRACT_SYSTEM) -> str:
    """Names the extraction prompt: changes whenever the system prompt or the output schema does."""
    schema = json.dumps(Extraction.model_json_schema(), sort_keys=True)
    return hashlib.sha256(f"{system}\n{schema}".encode()).hexdigest()[:12]


def client(timeout: float = 5) -> ollama.AsyncClient:
    return ollama.AsyncClient(host=settings.ollama_url, timeout=timeout)


async def models() -> Sequence[ollama.ListResponse.Model]:
    return (await client().list()).models


async def model_digest(model: str) -> str | None:
    """Ollama's digest of `model`, which tells apart two pulls of the same tag."""
    return next((m.digest for m in await models() if m.model == model), None)


async def _chat(
    messages: list[dict[str, Any]],
    *,
    fmt: dict[str, Any] | None = None,
    model: str | None = None,
    think: Think = False,
) -> str:
    # A 404 (ollama.ResponseError) usually means the model is still being pulled; Restate retries.
    resp = await client(settings.llm_timeout_s).chat(
        model=model or settings.llm_model,
        messages=messages,
        format=fmt,
        keep_alive="30m",
        **chat_params(think),
    )
    content = resp.message.content
    if not content or not content.strip():
        raise LlmError(f"ollama returned an empty message (done_reason={resp.done_reason})")
    return content


def document_message(received_at: datetime, subject: str | None, text: str) -> str:
    return (
        f"Document received: {received_at:%Y-%m-%d}\n"
        f"Subject: {subject or '(none)'}\n\n"
        f"{text or '(no text — see attached images)'}"
    )


OWNER_SAYS_TRAVEL = (
    "\n\nNote from the recipient: this document is a travel booking, though it was first read as not"
    " being one. Set is_travel_document to true and extract its reservations."
)
"""Added to a document whose owner said it is a booking (LlmExtractRequest.owner_says_travel)."""


async def extract(
    req: LlmExtractRequest, *, system: str = EXTRACT_SYSTEM, think: Think = False
) -> Extraction:
    """`system` and `think` are for evals trying another prompt or thinking; production uses neither."""
    doc = req.document
    user_msg: dict[str, Any] = {
        "role": "user",
        "content": document_message(req.received_at, doc.subject, doc.text)
        + (OWNER_SAYS_TRAVEL if req.owner_says_travel else ""),
    }
    if doc.images_b64:
        user_msg["images"] = doc.images_b64
    # Worked examples go first as earlier turns of the conversation, answered as the owner wanted.
    shown: list[dict[str, Any]] = []
    for ex in req.examples:
        shown.append({"role": "user", "content": document_message(ex.received_at, ex.subject, ex.text)})
        shown.append({"role": "assistant", "content": ex.answer.model_dump_json()})
    raw = await _chat(
        [{"role": "system", "content": system}, *shown, user_msg],
        fmt=Extraction.model_json_schema(),
        model=req.model,
        think=think,
    )
    try:
        return Extraction.model_validate_json(raw)
    except ValueError as e:
        # Structured output should prevent this, but a truncated generation can still happen.
        raise LlmError(f"invalid extraction JSON: {e}; raw={raw[:300]!r}") from e


async def embed(texts: list[str]) -> list[list[float]]:
    """EMBED_MODEL's vectors for `texts`, for comparing how alike they read. Ollama cuts each
    text to the model's context (2K tokens for embeddinggemma), which keeps its opening."""
    # embeddinggemma is trained with task prompts; this is the one for symmetric similarity.
    resp = await client(60).embed(
        model=settings.embed_model,
        input=[f"task: sentence similarity | query: {t}" for t in texts],
        keep_alive="30m",
    )
    if len(resp.embeddings) != len(texts):
        raise LlmError("ollama returned no embeddings")
    return [list(v) for v in resp.embeddings]


async def locate_subject(req: LlmSubjectRequest) -> SubjectBox:
    raw = await _chat(
        [
            {"role": "system", "content": SUBJECT_SYSTEM},
            {"role": "user", "content": f"A photo of {req.place}.", "images": [req.image_b64]},
        ],
        fmt=SubjectBox.model_json_schema(),
    )
    try:
        return SubjectBox.model_validate_json(raw)
    except ValueError as e:
        raise LlmError(f"invalid subject JSON: {e}; raw={raw[:300]!r}") from e


async def ask(req: LlmAskRequest) -> Answer:
    text = await _chat(
        [
            {"role": "system", "content": f"{ASK_SYSTEM}\n\nITINERARY:\n{req.context}"},
            {"role": "user", "content": req.question},
        ]
    )
    return Answer(text=text.strip())


async def health() -> dict[str, Any]:
    names = [m.model for m in await models()]
    return {"model": settings.llm_model, "available": settings.llm_model in names, "models": names}
