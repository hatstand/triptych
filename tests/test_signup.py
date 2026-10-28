"""Signing up at /signup: proving an address by mailing a code from it (signup.py)."""
# ruff: noqa: F811 (pytest fixtures imported from test_inbound are parameters here)

import re
from collections.abc import AsyncIterator
from dataclasses import replace

import httpx
import pytest

from tests.test_auth import Authenticator, sign_in
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
from tests.test_trip_members import a_trip
from triptych import auth, db, senders, sharing, signup
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
async def test_signup_is_off_unless_configured(
    browser: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(web, "settings", replace(web.settings, signups="closed"))
    assert (await browser.get("/signup")).status_code == 404
    assert (await browser.post("/signup", data={"email": "me@gmail.com"})).status_code == 404


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


async def create_passkey(browser: httpx.AsyncClient, device: Authenticator) -> httpx.Response:
    options = await browser.post("/auth/register/options", json={"signup": True})
    if options.status_code != 200:
        return options
    return await browser.post(
        "/auth/register/verify", json={"credential": device.create(options.json()), "name": "Phone"}
    )


@needs_db
@pytest.mark.anyio
async def test_a_verified_signup_becomes_an_account_with_its_passkey(
    browser: httpx.AsyncClient, mail: Mail, keys: tuple[bytes, bytes]
) -> None:
    device = Authenticator()
    code = await start(browser)
    # Not before the address is proven.
    early = await browser.post("/auth/register/options", json={"signup": True})
    assert early.status_code == 400 and "expired" in early.json()["error"]

    await mail(signed(code, "me@gmail.com", keys[0]), "me@gmail.com")
    page = (await browser.get("/signup")).text
    assert "is yours" in page and 'data-passkey="register" data-signup' in page

    done = await create_passkey(browser, device)
    assert done.status_code == 200 and done.json() == {"redirect": "/"}, done.text
    assert 'signup=""' in done.headers.get("set-cookie", "")  # cleared
    me = await senders.owner("me@gmail.com")
    assert me is not None
    (address,) = await senders.addresses(me)
    assert address.verified_at is not None
    assert [k.name for k in await auth.passkeys(me)] == ["Phone"]
    assert (await browser.get("/")).status_code == 200  # signed in

    # The signup is spent: no second account from it.
    assert (await browser.post("/auth/register/options", json={"signup": True})).status_code == 400
    browser.cookies.clear()
    assert (await sign_in(browser, device)).status_code == 200


@needs_db
@pytest.mark.anyio
async def test_an_address_with_an_account_is_told_to_sign_in(
    browser: httpx.AsyncClient, mail: Mail, keys: tuple[bytes, bytes]
) -> None:
    await db.user_for_email("me@gmail.com")
    code = await start(browser)
    await mail(signed(code, "me@gmail.com", keys[0]), "me@gmail.com")
    assert "already have an account" in (await browser.get("/signup")).text
    refused = await create_passkey(browser, Authenticator())
    assert refused.status_code == 400 and "Sign in instead" in refused.json()["error"]


@needs_db
@pytest.mark.anyio
async def test_trips_shared_with_the_address_wait_for_the_new_account(
    browser: httpx.AsyncClient, mail: Mail, keys: tuple[bytes, bytes]
) -> None:
    owner = await db.user_for_email("owner@example.com")
    trip_id = await a_trip(owner)
    await sharing.invite(owner, trip_id, "me@gmail.com")
    code = await start(browser)
    await mail(signed(code, "me@gmail.com", keys[0]), "me@gmail.com")
    assert (await create_passkey(browser, Authenticator())).status_code == 200
    me = await senders.owner("me@gmail.com")
    assert me is not None
    assert [i.trip_id for i in await sharing.pending_invites(me)] == [trip_id]


@pytest.fixture
def waitlist(monkeypatch: pytest.MonkeyPatch, browser: httpx.AsyncClient) -> None:
    for module in (web, signup):
        monkeypatch.setattr(module, "settings", replace(module.settings, signups="waitlist"))


@needs_db
@pytest.mark.anyio
async def test_on_the_waitlist_until_an_admin_lets_you_in(
    browser: httpx.AsyncClient,
    waitlist: None,
    mail: Mail,
    keys: tuple[bytes, bytes],
    fake_restate: FakeRestate,
) -> None:
    code = await start(browser)
    await mail(signed(code, "me@gmail.com", keys[0]), "me@gmail.com")
    assert (await create_passkey(browser, Authenticator())).status_code == 200
    me = await senders.owner("me@gmail.com")
    assert me is not None
    assert [w.email for w in await signup.waitlist()] == ["me@gmail.com"]

    home = await browser.get("/")
    assert home.status_code == 303 and home.headers["location"] == "/waitlist"
    assert (await browser.get("/account")).headers["location"] == "/waitlist"
    assert "on the waitlist" in (await browser.get("/waitlist")).text
    refused = await mail(sign(message("me@gmail.com"), "gmail.com", keys[0]), "me@gmail.com")
    assert refused.status_code == 403 and "waitlist" in refused.json()["detail"]
    assert fake_restate.sent == []

    await signup.approve(me)
    assert (await browser.get("/")).status_code == 200
    assert (await browser.get("/waitlist")).headers["location"] == "/"
    assert await signup.waitlist() == []


@needs_db
@pytest.mark.anyio
async def test_a_shared_trip_skips_the_waitlist(
    browser: httpx.AsyncClient, waitlist: None, mail: Mail, keys: tuple[bytes, bytes]
) -> None:
    owner = await db.user_for_email("owner@example.com")
    await sharing.invite(owner, await a_trip(owner), "me@gmail.com")
    code = await start(browser)
    await mail(signed(code, "me@gmail.com", keys[0]), "me@gmail.com")
    assert (await create_passkey(browser, Authenticator())).status_code == 200
    assert await signup.waitlist() == []
    assert (await browser.get("/")).status_code == 200


@needs_db
@pytest.mark.anyio
async def test_sign_in_page_offers_signing_up_only_when_its_on(
    browser: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert 'href="/signup"' in (await browser.get("/login")).text
    monkeypatch.setattr(web, "settings", replace(web.settings, signups="closed"))
    assert 'href="/signup"' not in (await browser.get("/login")).text


@needs_db
@pytest.mark.anyio
async def test_an_admin_invite_skips_the_waitlist(
    browser: httpx.AsyncClient, waitlist: None, mail: Mail, keys: tuple[bytes, bytes]
) -> None:
    invite = await auth.create_invite("me@gmail.com")
    assert (await browser.post("/signup", data={"invite": invite})).status_code == 303
    page = (await browser.get("/signup")).text
    assert "Use a different address" not in page  # the invite names it
    (code,) = set(CODE.findall(page))
    await mail(signed(code, "me@gmail.com", keys[0]), "me@gmail.com")
    assert (await create_passkey(browser, Authenticator())).status_code == 200
    assert await signup.waitlist() == []
    assert await auth.pending_invites() == []
