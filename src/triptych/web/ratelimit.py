"""Per-client and per-user request limits for the endpoints worth abusing once the UI is public:
the passkey ceremonies (anyone can call them) and the ones that queue LLM work.

Counters live in this process (the `limits` library's memory storage), which is right for a single
uvicorn process; a restart forgets them. The client is `request.client.host`, which uvicorn's
--proxy-headers fills in from Caddy's X-Forwarded-For (see compose/public.yaml)."""

import time

from fastapi import HTTPException
from limits import RateLimitItem, parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter

Limiter = MovingWindowRateLimiter

# Unauthenticated: per client address.
LOGIN = parse("20/minute")
REGISTER = parse("10/minute")
INVITE_PAGE = parse("30/minute")
# Signed in: per user. Each request queues an ingest or an LLM answer.
ADD_PLANS = parse("60/hour")
ASK = parse("60/hour")


def limiter() -> Limiter:
    return MovingWindowRateLimiter(MemoryStorage())


async def check(lim: Limiter, rate: RateLimitItem, scope: str, key: str) -> None:
    """Count one request against `rate` for (scope, key); 429 with Retry-After once it's spent."""
    if await lim.hit(rate, scope, key):
        return
    stats = await lim.get_window_stats(rate, scope, key)
    retry = max(1, round(stats.reset_time - time.time()))
    raise HTTPException(
        429, f"Too many requests; try again in {retry} seconds.", headers={"Retry-After": str(retry)}
    )
