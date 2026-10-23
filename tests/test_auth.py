"""Passkey sign-up and sign-in end to end, through the web app, with a software authenticator
standing in for the browser + device."""

import hashlib
import json
import os
import secrets
import struct
from collections.abc import AsyncIterator
from typing import Any

import httpx
import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from webauthn.helpers import base64url_to_bytes, bytes_to_base64url, encode_cbor

from triptych import auth, db
from triptych.config import settings
from triptych.web.app import SESSION_COOKIE, app

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set"),
]

ME = "me@example.com"
ORIGIN = settings.allowed_origins[0]


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
async def browser(database: None) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url=ORIGIN, headers={"Origin": ORIGIN}) as c:
        yield c


async def sign_up(browser: httpx.AsyncClient, device: Authenticator, email: str = ME) -> httpx.Response:
    invite = await auth.create_invite(email)
    page = await browser.get(f"/register/{invite}")
    assert page.status_code == 200 and email in page.text
    options = (await browser.post("/auth/register/options", json={"invite": invite})).json()
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
    # Mail filed for the address before it had an account stays with it.
    await db.file_reservations(ME, "ingest-1", [])
    device = Authenticator()
    resp = await sign_up(browser, device)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"redirect": "/"}
    assert (await browser.get("/api/overview")).status_code == 200

    await browser.post("/logout")
    assert SESSION_COOKIE not in browser.cookies
    assert (await browser.get("/")).status_code == 303

    resp = await sign_in(browser, device)
    assert resp.status_code == 200, resp.text
    page = await browser.get("/account")
    assert page.status_code == 200 and "Laptop" in page.text and "last used" in page.text


async def test_invite_works_once(browser: httpx.AsyncClient) -> None:
    invite = await auth.create_invite(ME)
    first = (await browser.post("/auth/register/options", json={"invite": invite})).json()
    second = (await browser.post("/auth/register/options", json={"invite": invite})).json()
    device = Authenticator()
    ok = await browser.post("/auth/register/verify", json={"credential": device.create(second)})
    assert ok.status_code == 200
    # The earlier ceremony for the same invite can't be completed now the invite is spent.
    resp = await browser.post("/auth/register/verify", json={"credential": Authenticator().create(first)})
    assert resp.status_code == 400
    assert (await browser.get(f"/register/{invite}")).status_code == 404


async def test_registration_without_invite_or_session_is_refused(browser: httpx.AsyncClient) -> None:
    resp = await browser.post("/auth/register/options", json={})
    assert resp.status_code == 400 and resp.json()["error"] == "Sign in first."
    resp = await browser.post("/auth/register/options", json={"invite": "made-up"})
    assert resp.status_code == 400


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


async def test_challenge_is_single_use(browser: httpx.AsyncClient) -> None:
    device = Authenticator()
    await sign_up(browser, device)
    challenge_id, options = await auth.start_login()
    assertion = device.get(json.loads(options))
    assert await auth.finish_login(challenge_id, assertion) == ME
    with pytest.raises(auth.AuthError, match="expired"):
        await auth.finish_login(challenge_id, assertion)


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

    keys = await auth.passkeys(ME)
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
    keys = await auth.passkeys(ME)
    resp = await browser.post(
        f"/account/passkeys/{keys[0].id}/delete", headers={"Origin": "https://evil.example"}
    )
    assert resp.status_code == 403
    assert (await browser.post("/logout", headers={"Origin": "https://evil.example"})).status_code == 403
    assert (await browser.get("/account")).status_code == 200
