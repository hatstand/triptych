from dataclasses import replace

import httpx
import pytest

from triptych.web import app as web


async def livez(monkeypatch: pytest.MonkeyPatch, base_url: str) -> httpx.Response:
    monkeypatch.setattr(web, "settings", replace(web.settings, public_base_url=base_url))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test") as c:
        return await c.get("/livez")


@pytest.mark.anyio
async def test_https_deployments_send_hsts(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await livez(monkeypatch, "https://triptych.example.com")
    assert resp.headers["strict-transport-security"] == "max-age=31536000"


@pytest.mark.anyio
async def test_plain_http_dev_sends_no_hsts(monkeypatch: pytest.MonkeyPatch) -> None:
    resp = await livez(monkeypatch, "http://localhost:8000")
    assert "strict-transport-security" not in resp.headers
