import base64
import os
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from dataclasses import replace
from typing import Any

import dkim
import httpx
import pytest

from triptych import db, inbound, senders
from triptych.models import IngestRequest
from triptych.web import app as web


@pytest.fixture(scope="module")
def keys() -> Iterator[tuple[bytes, bytes]]:
    """(private PEM, public key DER base64) generated per test run; never touches real DNS."""
    priv = subprocess.run(
        ["openssl", "genpkey", "-algorithm", "RSA", "-pkeyopt", "rsa_keygen_bits:2048"],
        capture_output=True,
        check=True,
    ).stdout
    der = subprocess.run(
        ["openssl", "pkey", "-pubout", "-outform", "DER"], input=priv, capture_output=True, check=True
    ).stdout
    yield priv, base64.b64encode(der)


def dns(pub: bytes, domains: set[str]) -> inbound.DnsFunc:
    def lookup(name: bytes, timeout: int = 5) -> bytes | None:
        domain = name.decode().rstrip(".").split("._domainkey.")[-1]
        return b"v=DKIM1; k=rsa; p=" + pub if domain in domains else None

    return lookup


def message(frm: str, body: str = "Booking ref QZ7K2M") -> bytes:
    return (
        f"From: {frm}\r\nTo: plans@triptych.purplehatstands.com\r\nSubject: Fwd: booking\r\n"
        f"Message-ID: <abc@example>\r\n\r\n{body}\r\n"
    ).encode()


def sign(raw: bytes, domain: str, priv: bytes) -> bytes:
    sig: bytes = dkim.sign(raw, b"s1", domain.encode(), priv, include_headers=[b"from", b"to", b"subject"])
    return sig + raw


def test_canonical_sender_undoes_gmail_autoforward_rewrite() -> None:
    assert (
        inbound.canonical_sender("Me <ME+caf_=plans=triptych.purplehatstands.com@gmail.com>")
        == "me@gmail.com"
    )
    assert inbound.canonical_sender("me+trips@gmail.com") == "me+trips@gmail.com"


def test_manual_forward_with_aligned_dkim_is_accepted(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    raw = sign(message("me@gmail.com"), "gmail.com", priv)
    result = inbound.verify(raw, "me@gmail.com", dns(pub, {"gmail.com"}))
    assert result == inbound.Accepted("me@gmail.com", "dkim:gmail.com")


def test_spoofed_sender_without_valid_signature_is_rejected(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    raw = sign(message("me@gmail.com"), "evil.example", priv)  # valid sig, wrong domain
    with pytest.raises(inbound.Rejected, match="could not authenticate"):
        inbound.verify(raw, "me@gmail.com", dns(pub, {"evil.example"}))


def test_signature_for_another_mailbox_at_the_domain_is_rejected(keys: tuple[bytes, bytes]) -> None:
    """Gmail signs anyone's mail as d=gmail.com, so a stranger can replay their own signed message
    with your address as the envelope sender. The signed From has to be the sender."""
    priv, pub = keys
    raw = sign(message("stranger@gmail.com"), "gmail.com", priv)
    with pytest.raises(inbound.Rejected, match="could not authenticate"):
        inbound.verify(raw, "me@gmail.com", dns(pub, {"gmail.com"}))


def test_a_second_from_header_is_rejected(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    raw = b"From: stranger@gmail.com\r\n" + sign(message("me@gmail.com"), "gmail.com", priv)
    with pytest.raises(inbound.Rejected):
        inbound.verify(raw, "me@gmail.com", dns(pub, {"gmail.com"}))
    assert inbound.author(raw) is None


def test_author_is_the_one_from_address() -> None:
    assert inbound.author(message("Me <Me@Gmail.com>")) == "me@gmail.com"
    assert inbound.author(message("a@gmail.com, b@gmail.com")) is None


def test_tampered_body_fails_dkim(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    raw = sign(message("me@gmail.com"), "gmail.com", priv).replace(b"QZ7K2M", b"XXXXXX")
    with pytest.raises(inbound.Rejected):
        inbound.verify(raw, "me@gmail.com", dns(pub, {"gmail.com"}))


AUTOFORWARD_ENVELOPE = "me+caf_=plans=triptych.purplehatstands.com@gmail.com"
# What a Gmail filter forward's own ARC set covers: the message, and the mailbox it was delivered to.
ARC_HEADERS = [b"from", b"to", b"subject", b"message-id", b"delivered-to", b"x-forwarded-for"]


def autoforward(priv: bytes, mailbox: str, body: str = "Booking ref QZ7K2M") -> bytes:
    """An airline's mail as Gmail forwards it from `mailbox`, with Google's ARC set on top."""
    raw = (
        f"X-Forwarded-For: {mailbox} plans@triptych.purplehatstands.com\r\n".encode()
        + f"Delivered-To: {mailbox}\r\n".encode()
        + b"Authentication-Results: mx.google.com; dkim=pass header.d=airline.example\r\n"
        + message("Atlantica Air <noreply@airline.example>", body)
    )
    seal = dkim.arc_sign(raw, b"arc", b"google.com", priv, b"mx.google.com", include_headers=ARC_HEADERS)
    return b"".join(seal) + raw


def test_autoforward_accepted_via_google_arc_seal(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    result = inbound.verify(autoforward(priv, "me@gmail.com"), AUTOFORWARD_ENVELOPE, dns(pub, {"google.com"}))
    assert result == inbound.Accepted("me@gmail.com", "arc:google.com")


def test_autoforward_from_another_mailbox_is_rejected(keys: tuple[bytes, bytes]) -> None:
    """A stranger auto-forwards from their own Gmail and resends it with your envelope."""
    priv, pub = keys
    with pytest.raises(inbound.Rejected):
        inbound.verify(
            autoforward(priv, "stranger@gmail.com"), AUTOFORWARD_ENVELOPE, dns(pub, {"google.com"})
        )


def test_autoforward_needs_google_to_have_signed_the_mailbox(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    lookup = dns(pub, {"google.com"})
    # A Delivered-To added on top after Google signed isn't one it covered.
    forged = autoforward(priv, "stranger@gmail.com")
    with pytest.raises(inbound.Rejected):
        inbound.verify(b"Delivered-To: me@gmail.com\r\n" + forged, AUTOFORWARD_ENVELOPE, lookup)
    # Nor is a seal whose message signature doesn't cover the forwarding headers at all.
    raw = b"Delivered-To: me@gmail.com\r\n" + message("noreply@airline.example")
    bare = b"".join(dkim.arc_sign(raw, b"arc", b"google.com", priv, b"mx.google.com")) + raw
    with pytest.raises(inbound.Rejected):
        inbound.verify(bare, AUTOFORWARD_ENVELOPE, lookup)
    # Nor is anyone else's seal.
    with pytest.raises(inbound.Rejected):
        inbound.verify(autoforward(priv, "me@gmail.com"), AUTOFORWARD_ENVELOPE, dns(pub, {"evil.example"}))


def test_gmail_forwarding_confirmation_surfaces_code(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    body = (
        "me@gmail.com has requested to automatically forward mail to your email address "
        "plans@triptych.purplehatstands.com.\r\nConfirmation code: 123456789\r\n"
        "https://mail-settings.google.com/mail/vf-abc123\r\n"
    )
    raw = sign(message("Gmail Team <forwarding-noreply@google.com>", body), "google.com", priv)
    result = inbound.verify(raw, "forwarding-noreply@google.com", dns(pub, {"google.com"}))
    assert result == inbound.ForwardingConfirmation(
        ("me@gmail.com", "plans@triptych.purplehatstands.com"),
        "123456789",
        "https://mail-settings.google.com/mail/vf-abc123",
    )


def test_forwarding_confirmation_must_be_from_the_verifier(keys: tuple[bytes, bytes]) -> None:
    """Any google.com-signed mail replayed with the verifier's envelope isn't a confirmation."""
    priv, pub = keys
    raw = sign(message("someone@google.com", "me@gmail.com\r\nConfirmation code: 1\r\n"), "google.com", priv)
    with pytest.raises(inbound.Rejected):
        inbound.verify(raw, "forwarding-noreply@google.com", dns(pub, {"google.com"}))


def test_verification_code_is_found_in_subject_or_body() -> None:
    raw = message("me@gmail.com").replace(b"Subject: Fwd: booking", b"Subject: Verify triptych-abcdefghjk")
    assert inbound.verification_code(raw) == "TRIPTYCH-ABCDEFGHJK"
    assert (
        inbound.verification_code(message("me@gmail.com", "code TRIPTYCH-ABCDEFGHJK"))
        == "TRIPTYCH-ABCDEFGHJK"
    )
    assert inbound.verification_code(message("me@gmail.com")) is None


# --- POST /inbound against a real Postgres: who may file, and verifying a new address ----------

TOKEN = "t0ken"
needs_db = pytest.mark.skipif(not os.environ.get("TEST_DATABASE_URL"), reason="TEST_DATABASE_URL not set")
Mail = Callable[[bytes, str], Awaitable[httpx.Response]]


class FakeRestate:
    def __init__(self) -> None:
        self.sent: list[IngestRequest] = []

    async def workflow_send(self, _handler: Any, _key: str, req: IngestRequest) -> None:
        self.sent.append(req)


@pytest.fixture
def fake_restate() -> Iterator[FakeRestate]:
    fake = FakeRestate()
    web.app.dependency_overrides[web.rs] = lambda: fake
    yield fake
    web.app.dependency_overrides.clear()


@pytest.fixture
async def mail(
    database: str, keys: tuple[bytes, bytes], fake_restate: FakeRestate, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[Mail]:
    """Delivers (raw, envelope sender) as the Email Worker would; keys are "published" for gmail.com
    and google.com."""
    real = inbound.verify
    lookup = dns(keys[1], {"gmail.com", "google.com"})
    monkeypatch.setattr(web.inbound, "verify", lambda raw, sender: real(raw, sender, lookup))
    monkeypatch.setattr(web, "settings", replace(web.settings, inbound_token=TOKEN))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=web.app), base_url="http://test") as c:

        async def deliver(raw: bytes, envelope_from: str) -> httpx.Response:
            headers = {"authorization": f"Bearer {TOKEN}", "x-envelope-from": envelope_from}
            return await c.post("/inbound", content=raw, headers=headers)

        yield deliver


@pytest.mark.anyio
@needs_db
async def test_mail_files_under_the_senders_user(
    mail: Mail, keys: tuple[bytes, bytes], fake_restate: FakeRestate
) -> None:
    me = await db.user_for_email("me@gmail.com")
    resp = await mail(sign(message("me@gmail.com"), "gmail.com", keys[0]), "me@gmail.com")
    assert resp.status_code == 200, resp.text
    assert [r.user for r in fake_restate.sent] == [me]


@pytest.mark.anyio
@needs_db
async def test_an_invited_address_is_verified_by_its_first_signed_mail(
    mail: Mail, keys: tuple[bytes, bytes]
) -> None:
    me = await db.user_for_email("me@gmail.com")  # as an admin invite makes it: unproven
    assert (await senders.addresses(me))[0].verified_at is None
    raw = autoforward(keys[0], "me@gmail.com", "Booking ref QZ7K2M")
    assert (await mail(raw, "me+caf_=plans=triptych.purplehatstands.com@gmail.com")).status_code == 200
    assert (await senders.addresses(me))[0].verified_at is None  # Gmail vouches for the route, not you
    assert (
        await mail(sign(message("me@gmail.com"), "gmail.com", keys[0]), "me@gmail.com")
    ).status_code == 200
    assert (await senders.addresses(me))[0].verified_at is not None


@pytest.mark.anyio
@needs_db
async def test_unknown_sender_is_refused_before_authenticating(mail: Mail, fake_restate: FakeRestate) -> None:
    resp = await mail(message("stranger@gmail.com"), "stranger@gmail.com")  # unsigned, too
    assert resp.status_code == 403
    assert "isn't a Triptych sender" in resp.json()["detail"]
    assert fake_restate.sent == []


@pytest.mark.anyio
@needs_db
async def test_new_address_is_verified_by_mailing_its_code(
    mail: Mail, keys: tuple[bytes, bytes], fake_restate: FakeRestate
) -> None:
    priv = keys[0]
    me = await db.user_for_email("me@example.com")
    code = await senders.request(me, "Me@Gmail.com")
    assert [p.email for p in await senders.pending(me)] == ["me@gmail.com"]

    # The code only vouches for the address it was issued for...
    other = sign(message("other@gmail.com", f"Verify {code}"), "gmail.com", priv)
    assert (await mail(other, "other@gmail.com")).status_code == 403
    # ...and the mail carrying it must still authenticate.
    assert (await mail(message("me@gmail.com", f"Verify {code}"), "me@gmail.com")).status_code == 403

    resp = await mail(sign(message("me@gmail.com", f"Verify {code}"), "gmail.com", priv), "me@gmail.com")
    assert resp.status_code == 200 and resp.json()["status"] == "sender-verified", resp.text
    assert fake_restate.sent == []  # the verification mail itself isn't a booking
    assert await senders.owner("me@gmail.com") == me
    assert [s.verified_at is not None for s in await senders.addresses(me)] == [False, True]
    assert await senders.pending(me) == []
    assert "will now be filed" in (await db.notifications(me))[0].text

    # Spent: from now on mail from the address is simply filed.
    resp = await mail(sign(message("me@gmail.com"), "gmail.com", priv), "me@gmail.com")
    assert resp.json()["status"] == "queued"
    assert [r.user for r in fake_restate.sent] == [me]


@pytest.mark.anyio
@needs_db
async def test_a_code_from_a_strangers_signed_mail_proves_nothing(
    mail: Mail, keys: tuple[bytes, bytes], fake_restate: FakeRestate
) -> None:
    """Someone asks for a code for your unclaimed address, then replays mail Gmail signed for them."""
    attacker = await db.user_for_email("attacker@gmail.com")
    code = await senders.request(attacker, "victim@gmail.com")
    raw = sign(message("attacker@gmail.com", f"Verify {code}"), "gmail.com", keys[0])
    assert (await mail(raw, "victim@gmail.com")).status_code == 403
    assert await senders.owner("victim@gmail.com") is None


@pytest.mark.anyio
@needs_db
async def test_a_code_is_not_accepted_by_auto_forward(
    mail: Mail, keys: tuple[bytes, bytes], fake_restate: FakeRestate
) -> None:
    """Codes come straight from the address, never by auto-forward, even a genuine one from it."""
    owner = await db.user_for_email("owner@gmail.com")
    code = await senders.request(owner, "victim@gmail.com")
    raw = autoforward(keys[0], "victim@gmail.com", f"Verify {code}")
    resp = await mail(raw, "victim+caf_=plans=triptych.purplehatstands.com@gmail.com")
    assert resp.status_code == 403 and "directly" in resp.json()["detail"]
    assert await senders.owner("victim@gmail.com") is None


@pytest.mark.anyio
@needs_db
async def test_an_address_belongs_to_one_user(database: str) -> None:
    me = await db.user_for_email("me@example.com")
    await db.user_for_email("taken@gmail.com")
    with pytest.raises(senders.SenderError, match="another Triptych account"):
        await senders.request(me, "taken@gmail.com")
    with pytest.raises(senders.SenderError, match="already one of your"):
        await senders.request(me, "me@example.com")
    with pytest.raises(senders.SenderError, match="only address"):
        await senders.remove(me, "me@example.com")


@pytest.mark.anyio
@needs_db
async def test_forwarding_confirmation_goes_to_the_requesting_sender(
    mail: Mail, keys: tuple[bytes, bytes]
) -> None:
    def confirmation(requester: str) -> bytes:
        body = f"{requester} has requested to automatically forward mail.\r\nConfirmation code: 42\r\n"
        return sign(message("forwarding-noreply@google.com", body), "google.com", keys[0])

    assert (
        await mail(confirmation("stranger@gmail.com"), "forwarding-noreply@google.com")
    ).status_code == 403
    me = await db.user_for_email("me@gmail.com")
    resp = await mail(confirmation("me@gmail.com"), "forwarding-noreply@google.com")
    assert resp.status_code == 200 and resp.json()["user"] == me
    assert "code 42" in (await db.notifications(me))[0].text
