"""The live-update stream, without a database: the hub is fed by hand."""

import asyncio

import pytest
from fastapi.sse import ServerSentEvent

from triptych.web import live

pytestmark = pytest.mark.anyio


class Page:
    def __init__(self) -> None:
        self.sections = {"inbox": "<p>empty</p>", "trips": "<p>none</p>"}
        self.renders = 0

    async def render(self) -> dict[str, str]:
        self.renders += 1
        return dict(self.sections)


@pytest.fixture(autouse=True)
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "SETTLE", 0)


def sent(e: ServerSentEvent) -> tuple[str | None, str | None]:
    return e.event, e.raw_data


async def test_starts_with_everything_then_sends_only_what_changed() -> None:
    hub, page = live.Hub("postgresql://unused"), Page()
    s = live.stream(hub, "u1", page.render)
    assert (await anext(s)).retry == live.RETRY_MS
    assert [sent(await anext(s)), sent(await anext(s))] == list(page.sections.items())

    page.sections["inbox"] = "<p>queued</p>"
    nxt = asyncio.ensure_future(anext(s))
    await asyncio.sleep(0)
    hub.changed("u2")  # someone else's data: nothing for u1
    await asyncio.sleep(0.01)
    assert not nxt.done()
    hub.changed("u1")
    assert sent(await nxt) == ("inbox", "<p>queued</p>")
    await s.aclose()


async def test_resync_with_no_changes_sends_nothing() -> None:
    hub, page = live.Hub("postgresql://unused"), Page()
    s = live.stream(hub, "u1", page.render)
    for _ in range(3):
        await anext(s)
    nxt = asyncio.ensure_future(anext(s))
    hub.resync()
    await asyncio.sleep(0.01)
    assert page.renders == 2  # rendered again, but nothing differed
    assert not nxt.done()
    nxt.cancel()


async def test_closed_streams_unsubscribe() -> None:
    hub, page = live.Hub("postgresql://unused"), Page()
    s = live.stream(hub, "u1", page.render)
    await anext(s)
    await s.aclose()
    assert not hub._streams
