import re
from pathlib import Path

import httpx
import pytest

from triptych.admin import app as admin
from triptych.web import app as web
from triptych.web import csp

TEMPLATES = Path(web.__file__).parent / "templates", Path(admin.__file__).parent / "templates"
TEMPLATE_FILES = [p for d in TEMPLATES for p in sorted(d.glob("*.html"))]
# What the policy refuses: an inline <script> or <style>, a style="" attribute, an on*="" handler.
INLINE = re.compile(r"<script(?![^>]*\ssrc=)[^>]*>|<style\b|\sstyle=|\son[a-z]+=", re.IGNORECASE)


@pytest.mark.anyio
async def test_web_sends_csp() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test") as c:
        resp = await c.get("/livez")
    assert resp.headers["content-security-policy"] == csp.POLICY


@pytest.mark.anyio
async def test_admin_sends_csp() -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=admin.app), base_url="http://test") as c:
        resp = await c.get("/admin")  # refused without a Tailscale identity, but still covered
    assert resp.headers["content-security-policy"] == csp.POLICY


def test_policy_allows_nothing_inline() -> None:
    assert "unsafe" not in csp.POLICY


@pytest.mark.parametrize("template", TEMPLATE_FILES, ids=lambda p: p.name)
def test_templates_have_no_inline_code(template: Path) -> None:
    assert INLINE.findall(template.read_text()) == []
