"""Restate service endpoint. Serves the handlers over HTTP/2 and, on every start, (re-)registers
itself with the Restate admin API and installs the flow-control rules. Doing this at boot means a
code reload under Tilt picks up new handler signatures and an LLM_CONCURRENCY change takes effect.
It then asks for photos of any places that don't have one yet (trips from before photos, lookups
that ran out of retries), or whose photo's subject hasn't been found."""

import asyncio
import logging
from typing import cast

import httpx
import restate
from hypercorn.asyncio import serve
from hypercorn.config import Config
from hypercorn.typing import ASGIFramework
from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware
from restate.server_types import RestateAppT

from triptych import db, telemetry
from triptych.config import settings
from triptych.models import PhotoRequest
from triptych.services import place_photo
from triptych.services.endpoint import app
from triptych.services.llm_service import GPU_SCOPE

log = logging.getLogger("triptych.worker")


RULES = [
    {
        "pattern": GPU_SCOPE,
        "limits": {"concurrency": settings.llm_concurrency},
        "description": "concurrent Gemma generations on the local GPU",
    }
]


async def register() -> bool:
    body = {"uri": settings.worker_public_url, "force": True}
    async with httpx.AsyncClient(base_url=settings.restate_admin_url, timeout=10) as client:
        for attempt in range(60):
            try:
                resp = await client.post("/deployments", json=body)
                resp.raise_for_status()
                services = [s["name"] for s in resp.json().get("services", [])]
                log.info("registered %s with restate: %s", settings.worker_public_url, services)
                resp = await client.put("/limits/rules", json=RULES)
                resp.raise_for_status()
                log.info("flow-control rules: %s", resp.json())
                return True
            except httpx.HTTPStatusError as e:
                log.warning("restate admin rejected request: %s %s", e, e.response.text[:300])
            except httpx.HTTPError as e:
                log.info("waiting for restate admin (%s)… %s", attempt, e)
            await asyncio.sleep(2)
    log.error("gave up registering with restate")
    return False


async def fill_timezones() -> None:
    try:
        await db.refill_timezones()
    except Exception:
        log.exception("couldn't fill in missing timezones")


async def request_photos() -> None:
    try:
        await db.fill_places()
        missing = await db.places_needing_photos()
        async with restate.create_client(settings.restate_ingress_url) as client:
            for place in missing:
                await client.object_send(place_photo.find, place_photo.key(place), PhotoRequest(place=place))
        if missing:
            log.info("asked for photos of %d places", len(missing))
    except Exception:
        log.exception("couldn't ask for missing photos")


def traced(endpoint: RestateAppT) -> ASGIFramework:
    """Restate sends each attempt's trace context (W3C traceparent) as request headers, so a server
    span per attempt puts everything the handler does (Ollama, Postgres, photo lookups) into the
    invocation's trace.
    Per-message send/receive spans would be noise on these long-lived HTTP/2 streams."""
    middleware = OpenTelemetryMiddleware(
        endpoint, excluded_urls="/discover,/health", exclude_spans=["receive", "send"]
    )
    # Typed with plain mappings rather than hypercorn's TypedDicts; it is an ASGI 3 app all the same.
    return cast(ASGIFramework, middleware)


async def main() -> None:
    with telemetry.running("triptych-worker"):
        config = Config()
        config.bind = ["0.0.0.0:9080"]
        # A Logger, not "-", so hypercorn logs through ours instead of adding a plain-text handler.
        config.errorlog = logging.getLogger("hypercorn.error")
        server = asyncio.create_task(serve(traced(app), config))
        await asyncio.sleep(0.5)
        if await register():
            await fill_timezones()
            await request_photos()
        await server


if __name__ == "__main__":
    asyncio.run(main())
