"""`Assistant` service: answer questions about one trip with the local LLM."""

import restate

from triptych import db
from triptych.models import Answer, AskTrip, LlmAskRequest, Trip
from triptych.services.llm_service import GPU_SCOPE, limit_key
from triptych.services.llm_service import ask as llm_ask

assistant = restate.Service("Assistant")


def itinerary_text(t: Trip) -> str:
    lines = [f"Trip: {t.name}"]
    for r in t.reservations:
        when = f"{r.start_local:%a %d %b %Y %H:%M}"
        if r.end_local:
            when += f" → {r.end_local:%a %d %b %H:%M}"
        bits = [r.kind.value.upper(), r.title, when]
        for label, val in (
            ("provider", r.provider),
            ("confirmation", r.confirmation_code),
            ("from", r.start_location),
            ("to", r.end_location),
            ("address", r.address),
            ("seat", r.seat),
            ("notes", r.notes),
        ):
            if val:
                bits.append(f"{label}: {val}")
        lines.append(" | ".join(bits))
    return "\n".join(lines)


async def _itinerary(owner: str, trip_id: str) -> str:
    t = await db.get_trip(owner, trip_id)
    if t is None:
        raise restate.TerminalError("trip not found", status_code=404)
    return itinerary_text(t)


@assistant.handler()
async def ask(ctx: restate.Context, req: AskTrip) -> Answer:
    context = await ctx.run_typed(
        "load itinerary", _itinerary, restate.RunOptions[str](), req.owner, req.trip_id
    )
    return await ctx.scope(GPU_SCOPE).service_call(
        llm_ask,
        LlmAskRequest(context=context, question=req.question),
        limit_key=limit_key("interactive", req.owner),
    )
