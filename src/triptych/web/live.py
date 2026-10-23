"""Live updates for open pages, over Server-Sent Events.

    db write ─ pg_notify(user) ─▶ Hub (one LISTEN connection per web process) ─▶ that user's streams

A notification only says *whose* data changed. Each stream then re-renders its page's sections and
sends the ones whose HTML differs from what it sent last, so the browser never holds a second copy
of the templates and a burst of changes (one ingest moving through its states) costs one render
per state at most. A new stream starts with a full snapshot, which is also how a reconnecting tab
catches up on whatever it missed.
"""

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import AsyncGenerator, Awaitable, Callable, Generator
from contextlib import contextmanager

import psycopg

from triptych import db

log = logging.getLogger("triptych.web.live")

KEEPALIVE = 20.0  # seconds; keeps proxies (Tailscale Serve, Funnel) from closing an idle stream
SETTLE = 0.1  # seconds to let back-to-back commits land before rendering once for all of them
RETRY_MS = 3000  # how long EventSource waits before reconnecting after a dropped stream

Render = Callable[[], Awaitable[dict[str, str]]]


class Hub:
    def __init__(self) -> None:
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
                async for user in db.changes():
                    if user is None:
                        delay = 1.0
                        self.resync()
                    else:
                        self.changed(user)
            except (psycopg.Error, OSError) as e:
                log.warning("lost the change feed (%s); retrying in %.0fs", e, delay)
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30.0)


hub = Hub()


def message(event: str, data: object) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


async def stream(user: str, render: Render, h: Hub = hub) -> AsyncGenerator[str]:
    """The body of one /events response: `update` events carrying {"sections": {name: html}}."""
    with h.subscribe(user) as ev:
        yield f"retry: {RETRY_MS}\n\n"
        sent: dict[str, str] = {}
        while True:
            try:
                await asyncio.wait_for(ev.wait(), KEEPALIVE)
            except TimeoutError:
                yield ": keepalive\n\n"
                continue
            await asyncio.sleep(SETTLE)
            ev.clear()
            sections = await render()
            fresh = {k: v for k, v in sections.items() if sent.get(k) != v}
            sent = sections
            if fresh:
                yield message("update", {"sections": fresh})
