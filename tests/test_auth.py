"""Passkey sign-up and sign-in end to end, through the web app, with a software authenticator
standing in for the browser + device."""

import base64
import hashlib
import json
import os
import re
import secrets
import struct
import time
import uuid
from collections.abc import AsyncIterator
from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, encode_cbor

from triptych import auth, db, signup
from triptych.config import settings
from triptych.models import Kind, Reservation
from triptych.web import ratelimit
from triptych.web.app import app
from triptych.web.deps import SESSION_COOKIE, limiter

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

ME = "me@example.com"
ORIGIN = settings.allowed_origins[0]


def hotel() -> Reservation:
    return Reservation(
        id=str(uuid.uuid4()),
        kind=Kind.LODGING,
        title="Hotel",
        start_local=datetime(2026, 11, 13, 15),
        end_local=datetime(2026, 11, 17, 11),
        city="Lisbon",
    )


def b64(b: bytes) -> str:
    return bytes_to_base64url(b)


class Authenticator:
    """Just enough of a platform authenticator: P-256 keys, "none" attestation, UP+UV."""

    def __init__(self, origin: str = ORIGIN) -> None:
        self.origin = origin
        self.creds: dict[bytes, tuple[ec.EllipticCurvePrivateKey, bytes]] = {}  # id → (key, user handle)
        self.counter = 0

    def _auth_data(self, flags: int, attested: bytes = b"") -> bytes:
        self.counter += 1
        rp_hash = hashlib.sha256(settings.webauthn_rp_id.encode()).digest()
        return rp_hash + bytes([flags]) + struct.pack(">I", self.counter) + attested

    def _client_data(self, kind: str, challenge: str) -> bytes:
        return json.dumps({"type": kind, "challenge": challenge, "origin": self.origin}).encode()

    def create(self, options: dict[str, Any]) -> dict[str, Any]:
        assert options["rp"]["id"] == settings.webauthn_rp_id
        assert options["authenticatorSelection"]["residentKey"] == "required"
        excluded = {base64url_to_bytes(c["id"]) for c in options.get("excludeCredentials", [])}
        assert not excluded & self.creds.keys(), "browser would refuse: already registered"
        key = ec.generate_private_key(ec.SECP256R1())
        nums = key.public_key().public_numbers()
        cose = encode_cbor({1: 2, 3: -7, -1: 1, -2: nums.x.to_bytes(32), -3: nums.y.to_bytes(32)})
        cred_id = secrets.token_bytes(16)
        self.creds[cred_id] = (key, base64url_to_bytes(options["user"]["id"]))
        attested = bytes(16) + struct.pack(">H", len(cred_id)) + cred_id + cose
        auth_data = self._auth_data(0x01 | 0x04 | 0x40, attested)  # UP, UV, attested credential data
        return {
            "id": b64(cred_id),
            "rawId": b64(cred_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(self._client_data("webauthn.create", options["challenge"])),
                "attestationObject": b64(encode_cbor({"fmt": "none", "attStmt": {}, "authData": auth_data})),
                "transports": ["internal"],
            },
            "clientExtensionResults": {},
        }

    def get(self, options: dict[str, Any], cred_id: bytes | None = None) -> dict[str, Any]:
        cred_id = cred_id or next(iter(self.creds))
        key, user_handle = self.creds[cred_id]
        auth_data = self._auth_data(0x01 | 0x04)
        client_data = self._client_data("webauthn.get", options["challenge"])
        sig = key.sign(auth_data + hashlib.sha256(client_data).digest(), ec.ECDSA(hashes.SHA256()))
        return {
            "id": b64(cred_id),
            "rawId": b64(cred_id),
            "type": "public-key",
            "response": {
                "clientDataJSON": b64(client_data),
                "authenticatorData": b64(auth_data),
                "signature": b64(sig),
                "userHandle": b64(user_handle),
            },
            "clientExtensionResults": {},
        }


@pytest.fixture
async def browser(database: str) -> AsyncIterator[httpx.AsyncClient]:
    # The lifespan doesn't run here, so give the app the rate limiter it would have made.
    lim = ratelimit.limiter()
    app.dependency_overrides[limiter] = lambda: lim
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        yield c
    app.dependency_overrides.clear()


async def accept_invite(browser: httpx.AsyncClient, invite: str, email: str = ME) -> None:
    """Opens the invite link and proves the address, as mailing its code to plans@ would (signup.py)."""
    page = await browser.get(f"/register/{invite}")
    assert page.status_code == 200 and email in page.text
    started = await browser.post("/signup", data={"invite": invite})
    assert started.status_code == 303, started.text
    (code,) = set(re.findall(r"TRIPTYCH-[A-Z0-9]{10}", (await browser.get("/signup")).text))
    await signup.confirm(code, email)


async def sign_up(browser: httpx.AsyncClient, device: Authenticator, email: str = ME) -> httpx.Response:
    await accept_invite(browser, await auth.create_invite(email), email)
    options = (await browser.post("/auth/register/options", json={"signup": True})).json()
    return await browser.post(
        "/auth/register/verify", json={"credential": device.create(options), "name": "Laptop"}
    )


async def sign_in(browser: httpx.AsyncClient, device: Authenticator, **kw: Any) -> httpx.Response:
    options = (await browser.post("/auth/login/options")).json()
    assert options["rpId"] == settings.webauthn_rp_id and not options.get("allowCredentials")
    return await browser.post("/auth/login/verify", json=device.get(options, **kw))


async def test_unauthenticated_requests_go_to_login(browser: httpx.AsyncClient) -> None:
    resp = await browser.get("/")
    assert resp.status_code == 303 and resp.headers["location"] == "/login"
    # The old prototype cookie means nothing now.
    browser.cookies.set("user", ME)
    assert (await browser.get("/api/overview")).status_code == 303


async def test_invite_sign_up_then_sign_in_with_passkey(browser: httpx.AsyncClient) -> None:
    # Mail filed for the address before anyone signed in as its user shows up after sign-up.
    owner = await db.user_for_email(ME)
    await db.file_reservations(owner, "ingest-1", [hotel()])
    device = Authenticator()
    resp = await sign_up(browser, device)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"redirect": "/"}
    [trip] = (await browser.get("/api/overview")).json()["trips"]
    assert trip["destination"] == "Lisbon"

    await browser.post("/logout")
    assert SESSION_COOKIE not in browser.cookies
    assert (await browser.get("/")).status_code == 303

    resp = await sign_in(browser, device)
    assert resp.status_code == 200, resp.text
    page = await browser.get("/account")
    assert page.status_code == 200 and "Laptop" in page.text and "last used" in page.text


async def test_invite_works_once(browser: httpx.AsyncClient) -> None:
    invite = await auth.create_invite(ME)
    await accept_invite(browser, invite)
    first = (await browser.post("/auth/register/options", json={"signup": True})).json()
    second = (await browser.post("/auth/register/options", json={"signup": True})).json()
    ok = await browser.post("/auth/register/verify", json={"credential": Authenticator().create(second)})
    assert ok.status_code == 200
    # The earlier ceremony for the same signup can't be completed now it and its invite are spent.
    resp = await browser.post("/auth/register/verify", json={"credential": Authenticator().create(first)})
    assert resp.status_code == 400
    assert (await browser.get(f"/register/{invite}")).status_code == 404
    assert (await browser.post("/signup", data={"invite": invite})).status_code == 404


async def test_an_invite_needs_the_address_proven(browser: httpx.AsyncClient) -> None:
    invite = await auth.create_invite(ME)
    assert (await browser.post("/signup", data={"invite": invite})).status_code == 303
    refused = await browser.post("/auth/register/options", json={"signup": True})
    assert refused.status_code == 400
    assert [i.email for i in await auth.pending_invites()] == [ME]


async def test_an_invite_works_while_signing_up_is_closed(browser: httpx.AsyncClient) -> None:
    assert settings.signups == "closed"
    assert (await browser.get("/signup")).status_code == 404
    assert (await sign_up(browser, Authenticator())).status_code == 200


async def test_pending_invites_hide_spent_and_expired_ones(browser: httpx.AsyncClient) -> None:
    await auth.create_invite("old@example.com", timedelta(seconds=-1))
    await auth.create_invite("new@example.com")
    assert [i.email for i in await auth.pending_invites()] == ["new@example.com"]


async def test_registration_without_invite_or_session_is_refused(browser: httpx.AsyncClient) -> None:
    resp = await browser.post("/auth/register/options", json={})
    assert resp.status_code == 400 and resp.json()["error"] == "Sign in first."
    resp = await browser.post("/auth/register/options", json={"signup": True})
    assert resp.status_code == 400
    assert (await browser.post("/signup", data={"invite": "made-up"})).status_code == 404


async def test_unknown_or_forged_passkey_cannot_sign_in(browser: httpx.AsyncClient) -> None:
    device = Authenticator()
    await sign_up(browser, device)
    await browser.post("/logout")

    assert (await sign_in(browser, device)).status_code == 200
    await browser.post("/logout")

    # Right credential id, wrong private key.
    [(cred_id, (_, handle))] = device.creds.items()
    forger = Authenticator()
    forger.creds[cred_id] = (ec.generate_private_key(ec.SECP256R1()), handle)
    resp = await sign_in(browser, forger)
    assert resp.status_code == 400 and "Sign-in failed" in resp.json()["error"]
    # A passkey this site never registered.
    stranger = Authenticator()
    stranger.creds[secrets.token_bytes(16)] = (ec.generate_private_key(ec.SECP256R1()), handle)
    assert (await sign_in(browser, stranger)).status_code == 400
    assert (await browser.get("/")).status_code == 303


async def test_assertion_for_another_origin_is_refused(browser: httpx.AsyncClient) -> None:
    device = Authenticator()
    await sign_up(browser, device)
    await browser.post("/logout")
    device.origin = "https://phish.example"
    resp = await sign_in(browser, device)
    assert resp.status_code == 400 and "origin" in resp.json()["error"]


async def test_ceremony_state_must_be_ours_fresh_and_for_this_step(browser: httpx.AsyncClient) -> None:
    device = Authenticator()
    await sign_up(browser, device)
    state, options = auth.start_login()
    assertion = device.get(json.loads(options))

    payload, _, sig = state.partition(".")
    forged = auth.Ceremony.model_validate_json(base64.urlsafe_b64decode(payload + "=="))
    forged.challenge = "AAAA"
    for bad in (None, "", payload + ".0" * 32, auth.seal(forged).partition(".")[0] + "." + sig):
        with pytest.raises(auth.AuthError, match="expired"):
            await auth.finish_login(bad, assertion)
    stale = auth.unseal(state, "login").model_copy(update={"expires": time.time() - 1})
    with pytest.raises(auth.AuthError, match="expired"):
        await auth.finish_login(auth.seal(stale), assertion)
    with pytest.raises(auth.AuthError, match="expired"):
        await auth.finish_registration(state, assertion, "x")

    assert await auth.finish_login(state, assertion) == await db.user_for_email(ME)


async def test_invite_for_another_address_of_the_same_user(browser: httpx.AsyncClient) -> None:
    owner = await db.user_for_email(ME)
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO user_email (email, user_id) VALUES ('alias@example.com', %s)", (owner,)
        )
    await sign_up(browser, Authenticator(), email="alias@example.com")
    assert [k.name for k in await auth.passkeys(owner)] == ["Laptop"]
    page = await browser.get("/account")
    assert ME in page.text  # shown by the user's first address


async def test_add_and_remove_passkeys_but_never_the_last(browser: httpx.AsyncClient) -> None:
    laptop = Authenticator()
    await sign_up(browser, laptop)
    phone = Authenticator()
    options = (await browser.post("/auth/register/options", json={})).json()
    assert len(options["excludeCredentials"]) == 1
    resp = await browser.post(
        "/auth/register/verify", json={"credential": phone.create(options), "name": "Phone"}
    )
    assert resp.json() == {"redirect": "/account"}

    keys = await auth.passkeys(await db.user_for_email(ME))
    assert [k.name for k in keys] == ["Laptop", "Phone"]
    assert (await browser.post(f"/account/passkeys/{keys[0].id}/delete")).status_code == 303
    resp = await browser.post(f"/account/passkeys/{keys[1].id}/delete")
    assert resp.status_code == 400 and "only passkey" in resp.text

    # The removed laptop passkey no longer works; the phone does.
    await browser.post("/logout")
    assert (await sign_in(browser, laptop)).status_code == 400
    assert (await sign_in(browser, phone)).status_code == 200


async def test_cross_origin_posts_are_refused(browser: httpx.AsyncClient) -> None:
    device = Authenticator()
    await sign_up(browser, device)
    keys = await auth.passkeys(await db.user_for_email(ME))
    resp = await browser.post(
        f"/account/passkeys/{keys[0].id}/delete", headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    assert resp.json()["error"] == (
        f"Requests from https://evil.example are refused; this server expects {ORIGIN} (PUBLIC_BASE_URL)."
    )
    assert (await browser.post("/logout", headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await browser.get("/account")).status_code == 200


async def test_account_page_adds_and_removes_sending_addresses(browser: httpx.AsyncClient) -> None:
    await sign_up(browser, Authenticator())
    page = await browser.post("/account/senders", data={"email": "Me@Work.example"})
    assert page.status_code == 200
    [code] = set(re.findall(r"TRIPTYCH-[A-Z0-9]{10}", page.text))
    assert "me@work.example" in page.text and "mailto:" in page.text
    # Codes are stored hashed: a later visit says it's pending but can't show the code again.
    page = await browser.get("/account/senders")
    assert "me@work.example" in page.text and code not in page.text and "New code" in page.text

    resp = await browser.post("/account/senders/delete", data={"email": "me@work.example"})
    assert resp.status_code == 303 and resp.headers["location"] == "/account/senders"
    assert "me@work.example" not in (await browser.get("/account/senders")).text

    refused = await browser.post("/account/senders/delete", data={"email": ME})
    assert refused.status_code == 400 and "only address" in refused.text


async def test_account_page_has_a_tab_per_setting(browser: httpx.AsyncClient) -> None:
    await sign_up(browser, Authenticator())
    passkeys = await browser.get("/account")
    assert 'href="/account" aria-current="page"' in passkeys.text and "Add a passkey" in passkeys.text
    assert "Add address" not in passkeys.text

    addresses = await browser.get("/account/senders")
    assert 'href="/account/senders" aria-current="page"' in addresses.text and "Add address" in addresses.text
    assert "Add a passkey" not in addresses.text and not re.search(r'src="[^"]*passkey', addresses.text)

    resp = await browser.get("/account/passkeys")
    assert resp.status_code == 308 and resp.headers["location"] == "/account"
    assert (await browser.get("/account/nope")).status_code == 404
