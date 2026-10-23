"""The live-update stream, without a database: the hub is fed by hand."""

import asyncio

import pytest

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


async def test_starts_with_everything_then_sends_only_what_changed() -> None:
    hub, page = live.Hub(), Page()
    s = live.stream("u1", page.render, hub)
    assert (await anext(s)).startswith("retry:")
    assert await anext(s) == live.message("update", {"sections": page.sections})

    page.sections["inbox"] = "<p>queued</p>"
    nxt = asyncio.ensure_future(anext(s))
    await asyncio.sleep(0)
    hub.changed("u2")  # someone else's data: nothing for u1
    await asyncio.sleep(0.01)
    assert not nxt.done()
    hub.changed("u1")
    assert await nxt == live.message("update", {"sections": {"inbox": "<p>queued</p>"}})
    await s.aclose()


async def test_resync_with_no_changes_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(live, "KEEPALIVE", 0.05)
    hub, page = live.Hub(), Page()
    s = live.stream("u1", page.render, hub)
    await anext(s)
    await anext(s)
    hub.resync()
    assert await anext(s) == ": keepalive\n\n"  # rendered again, but nothing differed
    assert page.renders == 2
    await s.aclose()


async def test_closed_streams_unsubscribe() -> None:
    hub, page = live.Hub(), Page()
    s = live.stream("u1", page.render, hub)
    await anext(s)
    await anext(s)
    await s.aclose()
    assert not hub._streams
