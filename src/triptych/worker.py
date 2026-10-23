"""Restate service endpoint. Serves the handlers over HTTP/2 and, on every start, (re-)registers
itself with the Restate admin API and installs the flow-control rules. Doing this at boot means a
code reload under Tilt picks up new handler signatures and an LLM_CONCURRENCY change takes effect."""

import asyncio
import logging

import httpx
from hypercorn.asyncio import serve
from hypercorn.config import Config

from triptych.config import settings
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


async def register() -> None:
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
                return
            except httpx.HTTPStatusError as e:
                log.warning("restate admin rejected request: %s %s", e, e.response.text[:300])
            except httpx.HTTPError as e:
                log.info("waiting for restate admin (%s)… %s", attempt, e)
            await asyncio.sleep(2)
    log.error("gave up registering with restate")


async def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    config = Config()
    config.bind = ["0.0.0.0:9080"]
    server = asyncio.create_task(serve(app, config))
    await asyncio.sleep(0.5)
    await register()
    await server


if __name__ == "__main__":
    asyncio.run(main())
