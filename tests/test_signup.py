"""Signing up at /signup: proving an address by mailing a code from it (signup.py)."""
# ruff: noqa: F811 (pytest fixtures imported from test_inbound are parameters here)

import re
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest

from tests.test_inbound import (  # noqa: F401 (fixtures)
    FakeRestate,
    Mail,
    autoforward,
    fake_restate,
    keys,
    mail,
    message,
    needs_db,
    sign,
)
from triptych import db, signup
from triptych.web import app as web
from triptych.web import live, ratelimit

ORIGIN = web.settings.allowed_origins[0]
CODE = re.compile(r"TRIPTYCH-[A-Z0-9]{10}")


@pytest.fixture
async def browser(database: str, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[httpx.AsyncClient]:
    monkeypatch.setattr(web, "settings", replace(web.settings, signups="open"))
    lim = ratelimit.limiter()
    web.app.dependency_overrides[web.limiter] = lambda: lim
    web.app.dependency_overrides[web.live_hub] = lambda: live.Hub(database)
    transport = httpx.ASGITransport(app=web.app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        yield c
    web.app.dependency_overrides.pop(web.limiter, None)
    web.app.dependency_overrides.pop(web.live_hub, None)


async def start(browser: httpx.AsyncClient, email: str = "me@gmail.com") -> str:
    resp = await browser.post("/signup", data={"email": email})
    assert resp.status_code == 303 and resp.headers["location"] == "/signup", resp.text
    page = await browser.get("/signup")
    (code,) = set(CODE.findall(page.text))
    return code


def signed(code: str, frm: str, priv: bytes) -> bytes:
    return sign(message(frm, f"Please sign me up. My code is {code}."), "gmail.com", priv)


@needs_db
@pytest.mark.anyio
async def test_signup_is_off_unless_configured(database: str) -> None:
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url=ORIGIN) as c:
        assert web.settings.signups == "closed"
        assert (await c.get("/signup")).status_code == 404
        assert (
            await c.post("/signup", data={"email": "me@gmail.com"}, headers={"Origin": ORIGIN})
        ).status_code == 404


@needs_db
@pytest.mark.anyio
async def test_mailing_the_code_proves_the_address(
    browser: httpx.AsyncClient, mail: Mail, keys: tuple[bytes, bytes], fake_restate: FakeRestate
) -> None:
    form = await browser.get("/signup")
    assert form.status_code == 200 and 'action="/signup"' in form.text

    code = await start(browser, " Me@Gmail.com ")
    page = (await browser.get("/signup")).text
    assert "me@gmail.com" in page and "mailto:plans@" in page and "mail.google.com" in page
    assert code in (await browser.get("/signup")).text  # the same code on every visit

    resp = await mail(signed(code, "me@gmail.com", keys[0]), "me@gmail.com")
    assert resp.status_code == 200 and resp.json()["status"] == "signup-verified", resp.text
    assert fake_restate.sent == []  # nothing is filed, and no account exists yet
    async with db.connection() as conn:
        assert await (await conn.execute("SELECT 1 FROM user_email")).fetchone() is None
    assert "is yours" in (await browser.get("/signup")).text


@needs_db
@pytest.mark.anyio
async def test_a_code_proves_only_its_own_address_and_only_by_dkim(
    browser: httpx.AsyncClient, mail: Mail, keys: tuple[bytes, bytes]
) -> None:
    code = await start(browser, "victim@gmail.com")
    # Someone else's signed mail carrying the code, sent with the victim's envelope or their own.
    assert (await mail(signed(code, "attacker@gmail.com", keys[0]), "victim@gmail.com")).status_code == 403
    assert (await mail(signed(code, "attacker@gmail.com", keys[0]), "attacker@gmail.com")).status_code == 403
    # Unsigned, or by auto-forward.
    assert (await mail(message("victim@gmail.com", code), "victim@gmail.com")).status_code == 403
    forwarded = autoforward(keys[0], "victim@gmail.com", code)
    resp = await mail(forwarded, "victim+caf_=plans=triptych.purplehatstands.com@gmail.com")
    assert resp.status_code == 403
    assert "is yours" not in (await browser.get("/signup")).text


@needs_db
@pytest.mark.anyio
async def test_starting_again_replaces_the_earlier_signup(
    browser: httpx.AsyncClient, mail: Mail, keys: tuple[bytes, bytes]
) -> None:
    first = await start(browser)
    second = await start(browser)
    assert first != second
    assert (await mail(signed(first, "me@gmail.com", keys[0]), "me@gmail.com")).status_code == 403
    assert (await mail(signed(second, "me@gmail.com", keys[0]), "me@gmail.com")).status_code == 200

    resp = await browser.post("/signup/cancel")
    assert resp.status_code == 303
    assert 'action="/signup"' in (await browser.get("/signup")).text


@needs_db
@pytest.mark.anyio
async def test_signup_looks_the_same_for_an_address_with_an_account(browser: httpx.AsyncClient) -> None:
    await db.user_for_email("taken@gmail.com")
    await start(browser, "taken@gmail.com")
    bad = await browser.post("/signup", data={"email": "not an address"})
    assert bad.status_code == 400 and "look like an email address" in bad.text
    assert 'value="not an address"' in bad.text and "/account" not in bad.text


@needs_db
@pytest.mark.anyio
async def test_the_event_stream_needs_a_signup(browser: httpx.AsyncClient) -> None:
    assert (await browser.get("/signup/events")).status_code == 404


def test_codes_are_derived_from_the_token() -> None:
    assert signup.code_for("a") == signup.code_for("a") != signup.code_for("b")
    assert CODE.fullmatch(signup.code_for("a"))
