"""Thin typed client for the local Ollama server (Gemma 4 by default).

Errors are raised, never swallowed: callers run these inside `ctx.run`, so Restate owns retries.
"""

from typing import Any

import httpx

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
- Car rentals: start = pickup, end = drop-off. Restaurants, tours, tickets, events: "restaurant"/"activity".
- Times are LOCAL to where they happen, formatted YYYY-MM-DDTHH:MM. If only a date is given use \
15:00 for hotel check-in, 11:00 for check-out, 09:00 otherwise.
- If the year is missing, infer the next occurrence on or after the date the document was received.
- For flights always fill start_iata/end_iata (3-letter airport codes) and flight_number.
- city is where the traveller will BE (the arrival city for transport, the hotel's city for lodging).
- Always fill timezone with the IANA timezone of that city (e.g. Asia/Shanghai for a train to Beijing).
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


async def _chat(messages: list[dict[str, Any]], *, fmt: dict[str, Any] | None = None) -> str:
    body: dict[str, Any] = {
        "model": settings.llm_model,
        "messages": messages,
        "stream": False,
        # Gemma 4 thinks by default; for extraction that mostly burns tokens (and can loop until
        # the context is exhausted), so turn it off and cap output to fail fast and retry instead.
        "think": False,
        "options": {"temperature": 0, "num_ctx": settings.llm_num_ctx, "num_predict": 4096},
        "keep_alive": "30m",
    }
    if fmt is not None:
        body["format"] = fmt
    async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=settings.llm_timeout_s) as client:
        resp = await client.post("/api/chat", json=body)
    if resp.status_code != 200:
        # 404 usually means the model is still being pulled; Restate will retry with backoff.
        raise LlmError(f"ollama {resp.status_code}: {resp.text[:500]}")
    data = resp.json()
    content = data.get("message", {}).get("content", "")
    if not isinstance(content, str) or not content.strip():
        raise LlmError(f"ollama returned an empty message (done_reason={data.get('done_reason')})")
    return content


async def extract(req: LlmExtractRequest) -> Extraction:
    doc = req.document
    user_msg: dict[str, Any] = {
        "role": "user",
        "content": (
            f"Document received: {req.received_at:%Y-%m-%d}\n"
            f"Subject: {doc.subject or '(none)'}\n\n"
            f"{doc.text or '(no text — see attached images)'}"
        ),
    }
    if doc.images_b64:
        user_msg["images"] = doc.images_b64
    raw = await _chat(
        [{"role": "system", "content": EXTRACT_SYSTEM}, user_msg],
        fmt=Extraction.model_json_schema(),
    )
    try:
        return Extraction.model_validate_json(raw)
    except ValueError as e:
        # Structured output should prevent this, but a truncated generation can still happen.
        raise LlmError(f"invalid extraction JSON: {e}; raw={raw[:300]!r}") from e


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
    async with httpx.AsyncClient(base_url=settings.ollama_url, timeout=5) as client:
        tags = (await client.get("/api/tags")).json()
    names = [m.get("name") for m in tags.get("models", [])]
    return {"model": settings.llm_model, "available": settings.llm_model in names, "models": names}
