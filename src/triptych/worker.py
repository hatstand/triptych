"""Restate service endpoint. Serves the handlers over HTTP/2 and, on every start, (re-)registers
itself with the Restate admin API and installs the flow-control rules. Doing this at boot means a
code reload under Tilt picks up new handler signatures and an LLM_CONCURRENCY change takes effect.
It then asks for photos of any places that don't have one yet (trips from before photos, lookups
that ran out of retries), or whose photo's subject hasn't been found."""

import asyncio
import logging
import signal
import socket

import httpx
import restate
from hypercorn.asyncio import serve
from hypercorn.config import Config

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


async def canonicalise_flight_numbers() -> None:
    try:
        await db.canonicalise_flight_numbers()
    except Exception:
        log.exception("couldn't canonicalise flight numbers")


async def fill_countries() -> None:
    try:
        await db.fill_countries()
    except Exception:
        log.exception("couldn't fill in countries")


async def retitle_trips() -> None:
    try:
        await db.retitle_trips()
    except Exception:
        log.exception("couldn't retitle trips")


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


async def start_up() -> None:
    if await register():
        await fill_timezones()
        await fill_countries()
        await canonicalise_flight_numbers()
        await retitle_trips()
        await request_photos()


async def main() -> None:
    telemetry.setup("triptych-worker")
    # Listen before serving, so Restate's call back during registration waits in the backlog until
    # hypercorn accepts it instead of racing the bind.
    sock = socket.create_server(("0.0.0.0", 9080))
    sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    config = Config()
    config.bind = [f"fd://{sock.fileno()}"]
    # A Logger, not "-", so hypercorn logs through ours instead of adding a plain-text handler.
    config.errorlog = logging.getLogger("hypercorn.error")
    # Stop on SIGINT, which compose sends this service (stop_signal). The Restate SDK takes over
    # SIGTERM once serving, only to tell running invocations, so on its own the worker never exits
    # and every deploy waits out Docker's stop timeout before killing it.
    stop = asyncio.Event()
    asyncio.get_running_loop().add_signal_handler(signal.SIGINT, stop.set)
    startup = asyncio.create_task(start_up())
    await serve(app, config, shutdown_trigger=stop.wait)
    startup.cancel()


if __name__ == "__main__":
    asyncio.run(main())
