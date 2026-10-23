"""`Llm` service: the only code that talks to the GPU, throttled by Restate flow control.

A Strix Halo iGPU comfortably runs exactly one Gemma 4 generation at a time. Instead of a
lock-style virtual object we route every call through the `gpu` *scope*:

    ctx.scope("gpu").service_call(extract, req, limit_key="batch/<user>")

and the worker installs a rule-book entry `gpu → concurrency = LLM_CONCURRENCY` on boot (see
worker.py). Restate's VQueue scheduler then holds excess invocations durably and admits them as
slots free up. Limit keys give each (lane, user) pair its own queue, so the scheduler interleaves
fairly: one user forwarding 40 emails doesn't starve another, and an interactive question doesn't
wait behind the whole batch backlog.
"""

import hashlib
from datetime import timedelta
from typing import Literal

import restate

from triptych import llm
from triptych.models import Answer, Extraction, LlmAskRequest, LlmExtractRequest

GPU_SCOPE = "gpu"

llm_service = restate.Service("Llm")

Lane = Literal["batch", "interactive"]


def limit_key(lane: Lane, user: str) -> str:
    # Limit-key levels must match [a-zA-Z0-9_.-]{1,36}; email addresses don't, so hash them.
    return f"{lane}/{hashlib.sha256(user.encode()).hexdigest()[:16]}"


# Generous: on first boot the model may still be downloading (16 GB), which surfaces as 404s.
EXTRACT_RETRY = restate.RunOptions[Extraction](
    max_duration=timedelta(minutes=45),
    initial_retry_interval=timedelta(seconds=2),
    max_retry_interval=timedelta(seconds=60),
    retry_interval_factor=2.0,
)
ASK_RETRY = restate.RunOptions[Answer](
    max_attempts=3,
    initial_retry_interval=timedelta(seconds=1),
)


@llm_service.handler(inactivity_timeout=timedelta(minutes=5), abort_timeout=timedelta(minutes=15))
async def extract(ctx: restate.Context, req: LlmExtractRequest) -> Extraction:
    return await ctx.run_typed("ollama extract", llm.extract, EXTRACT_RETRY, req)


@llm_service.handler(inactivity_timeout=timedelta(minutes=5), abort_timeout=timedelta(minutes=10))
async def ask(ctx: restate.Context, req: LlmAskRequest) -> Answer:
    return await ctx.run_typed("ollama ask", llm.ask, ASK_RETRY, req)
