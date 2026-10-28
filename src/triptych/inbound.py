"""Who is this email from, and can we prove it?

Mail reaches us from the internet via Cloudflare Email Routing → Email Worker → POST /inbound.
The *envelope sender* decides whose account a trip lands in, so it must be authenticated:

* Manual forward from Gmail: envelope = header From = you@gmail.com, DKIM-signed d=gmail.com.
* Gmail auto-forward (filter): envelope = you+caf_=plans=domain@gmail.com, header From is the
  airline, and Google adds an ARC seal (d=google.com) vouching for where it came from.

A signature only vouches for its domain, not for one mailbox in it: anyone at gmail.com can get
Gmail to sign a message and then send those bytes on with any envelope sender they like. So each
check also ties the signed message to the envelope sender's own mailbox:

* DKIM: the message has exactly one From, which is the envelope sender, and a valid signature that
  covers From and comes from that address's domain.
* ARC (Gmail only): the latest seal is Google's, the chain validates, and Google's latest
  ARC-Message-Signature covers a Delivered-To or X-Forwarded-For naming the envelope sender, i.e.
  the mailbox Gmail forwarded from.

Verification is done here on the raw bytes (DKIM/ARC survive the Worker untouched), not delegated
to Cloudflare. Whose account an authenticated sender files into (and whether they may at all) is
senders.py's business.
"""

import email
import email.policy
import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from email.utils import getaddresses, parseaddr
from typing import Any

import dkim
import dkim.util

from triptych import senders

# dkimpy's resolver hook: (name: bytes, timeout: int) -> TXT record bytes | None (untyped upstream).
DnsFunc = Callable[..., Any]

GOOGLE_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
GMAIL_FORWARDING_VERIFIER = "forwarding-noreply@google.com"


class Rejected(Exception):
    """Permanent rejection; the reason is shown to the sender in the bounce."""


@dataclass(frozen=True, slots=True)
class Accepted:
    sender: str
    how: str


@dataclass(frozen=True, slots=True)
class ForwardingConfirmation:
    """Gmail's 'confirm forwarding to plans@…' email: surface the code to the requesting user,
    who is whichever of `addresses` (every address in the body) is a verified sender."""

    addresses: tuple[str, ...]
    code: str | None
    link: str | None


def canonical_sender(addr: str) -> str:
    """Lower-case, and undo Gmail's auto-forward envelope rewrite (`you+caf_=…@gmail.com`)."""
    _, a = parseaddr(addr)
    local, _, domain = a.strip().lower().rpartition("@")
    local = re.sub(r"\+caf_=.*$", "", local)
    return f"{local}@{domain}" if local else ""


def ingest_id_for(raw: bytes, user: str) -> str:
    """Stable per (user, message): SMTP retries/re-POSTs dedupe, but two travellers forwarding the
    same confirmation each get it filed."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    message_id = str(msg.get("message-id") or "")
    return (
        "mail-" + hashlib.sha256(f"{user}\n{message_id or raw.decode('latin-1')}".encode()).hexdigest()[:24]
    )


def _signed_names(h: bytes) -> list[bytes]:
    """The header names a signature's h= tag lists, lower-cased, repeats kept."""
    return [name.strip().lower() for name in h.split(b":") if name.strip()]


def _dkim_domains(raw: bytes, dnsfunc: DnsFunc) -> set[str]:
    """Domains (d=) of every DKIM signature on the message that verifies and covers From."""
    d = dkim.DKIM(raw)
    count = sum(1 for name, _ in d.headers if name.lower() == b"dkim-signature")
    good: set[str] = set()
    for i in range(count):
        try:
            if (
                d.verify(idx=i, dnsfunc=dnsfunc)
                and d.domain
                and b"from" in _signed_names(d.signature_fields.get(b"h", b""))
            ):
                good.add(d.domain.decode().lower())
        except dkim.DKIMException:
            continue
    return good


def author(raw: bytes) -> str | None:
    """The one address in the message's one From header, lower-cased; None if it has more or fewer
    (a second From is how a signed message gets a different one shown)."""
    msg = email.message_from_bytes(raw, policy=email.policy.compat32)
    froms = msg.get_all("from") or []
    if len(froms) != 1:
        return None
    addresses = [a for _, a in getaddresses([str(froms[0])]) if a]
    return addresses[0].strip().lower() if len(addresses) == 1 else None


# The headers Gmail adds that name the mailbox a forward left from.
FORWARDING_HEADERS = (b"delivered-to", b"x-forwarded-for")


def _google_forwarded_for(raw: bytes, sender: str, dnsfunc: DnsFunc) -> bool:
    """Whether Google sealed the most recent ARC set, the chain validates, and the headers Google's
    message signature covers say the mail was forwarded from `sender`'s mailbox."""
    try:
        cv, results, _ = dkim.arc_verify(raw, dnsfunc=dnsfunc)
    except dkim.DKIMException:
        return False
    if cv != dkim.CV_Pass or not results:
        return False
    latest = results[0]
    if (
        latest.get("as-domain", b"").lower() != b"google.com"
        or latest.get("ams-domain", b"").lower() != b"google.com"
    ):
        return False
    headers = dkim.DKIM(raw).headers
    marker = b" i=%d;" % latest["instance"]
    ams = next(
        (v for n, v in headers if n.lower() == b"arc-message-signature" and marker in b" " + v.lower()), None
    )
    if ams is None:
        return False
    try:
        covered = _signed_names(dkim.util.parse_tag_value(ams.strip()).get(b"h", b""))
    except dkim.util.InvalidTagValueList:
        return False
    # The instances a verifier checks: each name listed picks the next one up from the bottom.
    signed = dkim.select_headers(headers, [n for n in covered if n in FORWARDING_HEADERS])
    named = {
        canonical_sender(a)
        for _, value in signed
        for a in re.split(r"[\s,]+", value.decode("utf-8", "replace"))
        if "@" in a
    }
    return sender in named


def _aligned(signer: str, domain: str) -> bool:
    return signer == domain or signer.endswith("." + domain)


def verification_code(raw: bytes) -> str | None:
    """A sender verification code (senders.py) in the subject or the text of the message."""
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    if code := senders.find_code(str(msg.get("subject") or "")):
        return code
    body = msg.get_body(preferencelist=("plain", "html"))
    return senders.find_code(body.get_content()) if body is not None else None


def verify(
    raw: bytes, envelope_from: str, dnsfunc: DnsFunc = dkim.get_txt
) -> Accepted | ForwardingConfirmation:
    """Blocking (DNS lookups for keys); call via asyncio.to_thread."""
    sender = canonical_sender(envelope_from)
    if sender == GMAIL_FORWARDING_VERIFIER:
        return _forwarding_confirmation(raw, dnsfunc)
    if not sender:
        raise Rejected("no sender address")

    domain = sender.rpartition("@")[2]
    if author(raw) == sender and any(_aligned(s, domain) for s in _dkim_domains(raw, dnsfunc)):
        return Accepted(sender, f"dkim:{domain}")
    if domain in GOOGLE_DOMAINS and _google_forwarded_for(raw, sender, dnsfunc):
        return Accepted(sender, "arc:google.com")
    raise Rejected(
        f"could not authenticate {sender} (it needs to be From {sender} with a DKIM signature from"
        f" {domain}, or a Gmail auto-forward from that mailbox)"
    )


def _forwarding_confirmation(raw: bytes, dnsfunc: DnsFunc) -> ForwardingConfirmation:
    if author(raw) != GMAIL_FORWARDING_VERIFIER or not any(
        _aligned(s, "google.com") for s in _dkim_domains(raw, dnsfunc)
    ):
        raise Rejected("forwarding confirmation not signed by google.com")
    msg = email.message_from_bytes(raw, policy=email.policy.default)
    body = msg.get_body(preferencelist=("plain", "html"))
    text = body.get_content() if body is not None else ""
    addresses = tuple(
        dict.fromkeys(a.lower().rstrip(".") for a in re.findall(r"[\w.+-]+@[\w-]+\.[\w.]+", text))
    )
    code = re.search(r"Confirmation code:\s*(\d+)", text)
    link = re.search(r"https://mail(?:-settings)?\.google\.com/\S+", text)
    return ForwardingConfirmation(
        addresses=addresses, code=code.group(1) if code else None, link=link.group(0) if link else None
    )
