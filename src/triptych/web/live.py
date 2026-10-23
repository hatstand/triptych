"""Live updates for open pages, over Server-Sent Events.

    db write ─ pg_notify(user) ─▶ Hub (one LISTEN connection per web process) ─▶ that user's streams

A notification only says *whose* data changed. Each stream then re-renders its page's sections and
sends the ones whose HTML differs from what it sent last, one SSE event per section named after it,
which htmx's SSE extension swaps into the matching `sse-swap` element. So the browser never holds a
second copy of the templates, and a burst of changes (one ingest moving through its states) costs
one render per state at most. A new stream starts with a full snapshot, which is also how a
reconnecting tab catches up on whatever it missed. FastAPI's EventSourceResponse does the framing
and keepalive pings.
"""

import asyncio
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import contextmanager

import psycopg
from fastapi.sse import ServerSentEvent

from triptych import db

log = logging.getLogger("triptych.web.live")

SETTLE = 0.1  # seconds to let back-to-back commits land before rendering once for all of them
RETRY_MS = 3000  # how long EventSource waits before reconnecting after a dropped stream

Render = Callable[[], Awaitable[dict[str, str]]]


class Hub:
    """Which streams are open for which user. One per app, made by its lifespan (web/app.py)."""

    def __init__(self, database_url: str) -> None:
        self._database_url = database_url
        self._streams: defaultdict[str, set[asyncio.Event]] = defaultdict(set)

    @contextmanager
    def subscribe(self, user: str) -> Generator[asyncio.Event]:
        """An event that is set whenever `user`'s data may have changed (and to start with)."""
        ev = asyncio.Event()
        ev.set()
        self._streams[user].add(ev)
        try:
            yield ev
        finally:
            self._streams[user].discard(ev)
            if not self._streams[user]:
                del self._streams[user]

    def changed(self, user: str) -> None:
        for ev in self._streams.get(user, ()):
            ev.set()

    def resync(self) -> None:
        for evs in self._streams.values():
            for ev in evs:
                ev.set()

    async def run(self) -> None:
        """Feed the hub from Postgres for as long as the app runs, reconnecting when the database
        goes away. Every (re)connect is a resync, since notifications sent meanwhile are lost."""
        delay = 1.0
        while True:
            try:
                async for user in db.changes(self._database_url):
                    if user is None:
                        delay = 1.0
                        self.resync()
                    else:
                        self.changed(user)
            except (psycopg.Error, OSError) as e:
                log.warning("lost the change feed (%s); retrying in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


async def stream(hub: Hub, user: str, render: Render) -> AsyncGenerator[ServerSentEvent]:
    """The events of one /events response: the HTML of each section that changed."""
    with hub.subscribe(user) as ev:
        yield ServerSentEvent(retry=RETRY_MS, comment="connected")
        sent: dict[str, str] = {}
        while True:
            await ev.wait()
            await asyncio.sleep(SETTLE)
            ev.clear()
            sections = await render()
            for name, html in sections.items():
                if sent.get(name) != html:
                    yield ServerSentEvent(event=name, raw_data=html)
            sent = sections
