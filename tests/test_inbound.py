import base64
import subprocess
from collections.abc import Iterator

import dkim
import pytest

from triptych import inbound

ALLOWED = frozenset({"me@gmail.com"})


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
        f"From: {frm}\r\nTo: plans@purplehatstands.com\r\nSubject: Fwd: booking\r\n"
        f"Message-ID: <abc@example>\r\n\r\n{body}\r\n"
    ).encode()


def sign(raw: bytes, domain: str, priv: bytes) -> bytes:
    sig: bytes = dkim.sign(raw, b"s1", domain.encode(), priv, include_headers=[b"from", b"to", b"subject"])
    return sig + raw


def test_canonical_sender_undoes_gmail_autoforward_rewrite() -> None:
    assert inbound.canonical_sender("Me <ME+caf_=plans=purplehatstands.com@gmail.com>") == "me@gmail.com"
    assert inbound.canonical_sender("me+trips@gmail.com") == "me+trips@gmail.com"


def test_manual_forward_with_aligned_dkim_is_accepted(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    raw = sign(message("me@gmail.com"), "gmail.com", priv)
    result = inbound.verify(raw, "me@gmail.com", ALLOWED, dns(pub, {"gmail.com"}))
    assert result == inbound.Accepted("me@gmail.com", "dkim:gmail.com")


def test_spoofed_sender_without_valid_signature_is_rejected(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    raw = sign(message("me@gmail.com"), "evil.example", priv)  # valid sig, wrong domain
    with pytest.raises(inbound.Rejected, match="could not authenticate"):
        inbound.verify(raw, "me@gmail.com", ALLOWED, dns(pub, {"evil.example"}))


def test_tampered_body_fails_dkim(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    raw = sign(message("me@gmail.com"), "gmail.com", priv).replace(b"QZ7K2M", b"XXXXXX")
    with pytest.raises(inbound.Rejected):
        inbound.verify(raw, "me@gmail.com", ALLOWED, dns(pub, {"gmail.com"}))


def test_unlisted_sender_is_rejected_before_crypto() -> None:
    with pytest.raises(inbound.Rejected, match="not allowed"):
        inbound.verify(message("x@y.com"), "x@y.com", ALLOWED, lambda *a, **k: None)


def test_autoforward_accepted_via_google_arc_seal(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    raw = b"Authentication-Results: mx.google.com; dkim=pass header.d=airline.example\r\n" + message(
        "Atlantica Air <noreply@airline.example>"
    )
    seal = b"".join(dkim.arc_sign(raw, b"arc", b"google.com", priv, b"mx.google.com"))
    result = inbound.verify(
        seal + raw, "me+caf_=plans=purplehatstands.com@gmail.com", ALLOWED, dns(pub, {"google.com"})
    )
    assert result == inbound.Accepted("me@gmail.com", "arc:google.com")


def test_gmail_forwarding_confirmation_surfaces_code(keys: tuple[bytes, bytes]) -> None:
    priv, pub = keys
    body = (
        "me@gmail.com has requested to automatically forward mail to your email address "
        "plans@purplehatstands.com.\r\nConfirmation code: 123456789\r\n"
        "https://mail-settings.google.com/mail/vf-abc123\r\n"
    )
    raw = sign(message("Gmail Team <forwarding-noreply@google.com>", body), "google.com", priv)
    result = inbound.verify(raw, "forwarding-noreply@google.com", ALLOWED, dns(pub, {"google.com"}))
    assert result == inbound.ForwardingConfirmation(
        "me@gmail.com", "123456789", "https://mail-settings.google.com/mail/vf-abc123"
    )
