"""Accounts, passkeys (WebAuthn) and sign-in sessions. Passkeys are the only way to sign in.

An account is an `app_user` (the address trips are filed under, and that inbound mail is matched
on) with at least one passkey. Mail can be filed for an address before it has an account; whoever
registers a passkey for that address then sees those trips. Nothing here proves control of the
address, so the first passkey for an address needs an invite (`python -m triptych.invite`), which
is also the recovery path for someone who has lost all theirs. Further passkeys are added while
signed in.
"""

import hashlib
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Any, Literal

import webauthn
from pydantic import BaseModel
from webauthn.helpers import (
    bytes_to_base64url,
    parse_authentication_credential_json,
    parse_registration_credential_json,
    structs,
)
from webauthn.helpers.exceptions import WebAuthnException

from triptych import db
from triptych.config import settings

SESSION_TTL = timedelta(days=30)
INVITE_TTL = timedelta(days=7)
CHALLENGE_TTL = timedelta(minutes=5)
RP_NAME = "Triptych"

# Every ceremony requires user verification (biometric or PIN): the passkey is the only factor.
UV = structs.UserVerificationRequirement.REQUIRED


class AuthError(Exception):
    """A ceremony or invite was refused. The message is safe to show the user."""


class Passkey(BaseModel):
    id: str  # base64url credential id
    name: str
    backed_up: bool
    created_at: datetime
    last_used_at: datetime | None


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def normalise_email(email: str) -> str:
    return email.strip().lower()


# --- invites ---------------------------------------------------------------------------------


async def create_invite(email: str, ttl: timedelta = INVITE_TTL) -> str:
    """A one-time token that lets its holder register a passkey for `email`."""
    token = secrets.token_urlsafe(32)
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO invite (token_hash, email, expires_at) VALUES (%s, %s, now() + %s)",
            (_hash(token), normalise_email(email), ttl),
        )
    return token


async def invite_email(token: str) -> str | None:
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT email FROM invite WHERE token_hash = %s AND expires_at > now()", (_hash(token),)
        )
        row = await cur.fetchone()
    return row[0] if row else None


# --- sessions --------------------------------------------------------------------------------


async def create_session(email: str) -> str:
    token = secrets.token_urlsafe(32)
    async with db.connection() as conn:
        await conn.execute("DELETE FROM web_session WHERE expires_at <= now()")
        await conn.execute(
            "INSERT INTO web_session (token_hash, email, expires_at) VALUES (%s, %s, now() + %s)",
            (_hash(token), email, SESSION_TTL),
        )
    return token


async def session_user(token: str) -> str | None:
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT email FROM web_session WHERE token_hash = %s AND expires_at > now()", (_hash(token),)
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def end_session(token: str) -> None:
    async with db.connection() as conn:
        await conn.execute("DELETE FROM web_session WHERE token_hash = %s", (_hash(token),))


# --- passkey management ----------------------------------------------------------------------


async def passkeys(email: str) -> list[Passkey]:
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT credential_id, name, backed_up, created_at, last_used_at FROM passkey
               WHERE email = %s ORDER BY created_at""",
            (email,),
        )
        return [
            Passkey(id=bytes_to_base64url(cid), name=name, backed_up=b, created_at=c, last_used_at=u)
            for cid, name, b, c, u in await cur.fetchall()
        ]


async def delete_passkey(email: str, passkey_id: str) -> None:
    """Refuses to remove someone's last passkey, which would lock them out."""
    async with db.connection() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM account WHERE email = %s FOR UPDATE", (email,))
        cur = await conn.execute("SELECT credential_id FROM passkey WHERE email = %s", (email,))
        ids = [bytes_to_base64url(row[0]) for row in await cur.fetchall()]
        if passkey_id not in ids:
            raise AuthError("No such passkey.")
        if len(ids) == 1:
            raise AuthError("That's your only passkey. Add another before removing it.")
        await conn.execute(
            "DELETE FROM passkey WHERE email = %s AND credential_id = %s",
            (email, webauthn.base64url_to_bytes(passkey_id)),
        )


# --- ceremonies ------------------------------------------------------------------------------


async def _new_challenge(
    purpose: Literal["register", "login"],
    challenge: bytes,
    email: str | None = None,
    user_id: bytes | None = None,
    invite_hash: bytes | None = None,
) -> str:
    cid = str(uuid.uuid4())
    async with db.connection() as conn:
        await conn.execute("DELETE FROM webauthn_challenge WHERE expires_at <= now()")
        await conn.execute(
            """INSERT INTO webauthn_challenge
                   (id, challenge, purpose, email, webauthn_user_id, invite_hash, expires_at)
               VALUES (%s, %s, %s, %s, %s, %s, now() + %s)""",
            (cid, challenge, purpose, email, user_id, invite_hash, CHALLENGE_TTL),
        )
    return cid


async def _take_challenge(
    challenge_id: str | None, purpose: str
) -> tuple[bytes, str | None, bytes | None, bytes | None]:
    """Each challenge works once, whether or not the ceremony that uses it succeeds."""
    try:
        uuid.UUID(challenge_id or "")
    except ValueError:
        raise AuthError("Sign-in expired. Please try again.") from None
    async with db.connection() as conn:
        cur = await conn.execute(
            """DELETE FROM webauthn_challenge WHERE id = %s AND purpose = %s AND expires_at > now()
               RETURNING challenge, email, webauthn_user_id, invite_hash""",
            (challenge_id, purpose),
        )
        row = await cur.fetchone()
    if row is None:
        raise AuthError("Sign-in expired. Please try again.")
    return row


async def start_registration(email: str, invite: str | None) -> tuple[str, str]:
    """(challenge id, PublicKeyCredentialCreationOptions JSON). Without an invite, `email` must be
    the signed-in user."""
    invite_hash = None
    if invite is not None:
        if await invite_email(invite) != email:
            raise AuthError("This invite has expired or was already used.")
        invite_hash = _hash(invite)
    async with db.connection() as conn:
        cur = await conn.execute("SELECT webauthn_user_id FROM account WHERE email = %s", (email,))
        row = await cur.fetchone()
        cur = await conn.execute("SELECT credential_id, transports FROM passkey WHERE email = %s", (email,))
        existing = await cur.fetchall()
    user_id = row[0] if row else secrets.token_bytes(32)
    options = webauthn.generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=RP_NAME,
        user_id=user_id,
        user_name=email,
        user_display_name=email,
        authenticator_selection=structs.AuthenticatorSelectionCriteria(
            resident_key=structs.ResidentKeyRequirement.REQUIRED, user_verification=UV
        ),
        exclude_credentials=[
            structs.PublicKeyCredentialDescriptor(
                id=cid, transports=[structs.AuthenticatorTransport(t) for t in transports]
            )
            for cid, transports in existing
        ],
    )
    cid = await _new_challenge("register", options.challenge, email, user_id, invite_hash)
    return cid, webauthn.options_to_json(options)


async def finish_registration(challenge_id: str | None, credential: dict[str, Any], name: str) -> str:
    """Stores the new passkey (creating the account if needed) and returns its owner."""
    challenge, email, user_id, invite_hash = await _take_challenge(challenge_id, "register")
    assert email is not None and user_id is not None
    try:
        parsed = parse_registration_credential_json(credential)
        v = webauthn.verify_registration_response(
            credential=parsed,
            expected_challenge=challenge,
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=list(settings.allowed_origins),
            require_user_verification=True,
        )
    except WebAuthnException as e:
        raise AuthError(f"That passkey couldn't be registered ({e}).") from e

    transports = [t.value for t in parsed.response.transports or []]
    async with db.connection() as conn, conn.transaction():
        if invite_hash is not None:
            cur = await conn.execute(
                "DELETE FROM invite WHERE token_hash = %s AND email = %s AND expires_at > now() RETURNING 1",
                (invite_hash, email),
            )
            if await cur.fetchone() is None:
                raise AuthError("This invite has expired or was already used.")
        await db.ensure_user(conn, email)
        await conn.execute(
            "INSERT INTO account (email, webauthn_user_id) VALUES (%s, %s) ON CONFLICT (email) DO NOTHING",
            (email, user_id),
        )
        cur = await conn.execute("SELECT webauthn_user_id FROM account WHERE email = %s", (email,))
        row = await cur.fetchone()
        if row is None or row[0] != user_id:
            # Two first registrations raced, and the other one created the account.
            raise AuthError("Please try again.")
        await conn.execute(
            """INSERT INTO passkey (credential_id, email, public_key, sign_count, transports, backed_up, name)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (credential_id) DO NOTHING""",
            (v.credential_id, email, v.credential_public_key, v.sign_count, transports,
             v.credential_backed_up, name.strip()[:60] or "Passkey"),
        )  # fmt: skip
    return email


async def start_login() -> tuple[str, str]:
    """Discoverable-credential sign-in: the browser offers whichever passkeys it has for us."""
    options = webauthn.generate_authentication_options(rp_id=settings.webauthn_rp_id, user_verification=UV)
    cid = await _new_challenge("login", options.challenge)
    return cid, webauthn.options_to_json(options)


async def finish_login(challenge_id: str | None, credential: dict[str, Any]) -> str:
    """Verifies the assertion and returns who signed in."""
    challenge, *_ = await _take_challenge(challenge_id, "login")
    try:
        parsed = parse_authentication_credential_json(credential)
    except WebAuthnException as e:
        raise AuthError("That passkey response was malformed.") from e
    async with db.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """SELECT p.email, p.public_key, p.sign_count, a.webauthn_user_id
               FROM passkey p JOIN account a USING (email) WHERE p.credential_id = %s FOR UPDATE OF p""",
            (parsed.raw_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise AuthError("That passkey isn't registered here. It may have been removed.")
        email, public_key, sign_count, user_id = row
        if parsed.response.user_handle is not None and parsed.response.user_handle != user_id:
            raise AuthError("That passkey doesn't match its account.")
        try:
            v = webauthn.verify_authentication_response(
                credential=parsed,
                expected_challenge=challenge,
                expected_rp_id=settings.webauthn_rp_id,
                expected_origin=list(settings.allowed_origins),
                credential_public_key=public_key,
                credential_current_sign_count=sign_count,
                require_user_verification=True,
            )
        except WebAuthnException as e:
            raise AuthError(f"Sign-in failed ({e}).") from e
        await conn.execute(
            """UPDATE passkey SET sign_count = %s, backed_up = %s, last_used_at = now()
               WHERE credential_id = %s""",
            (v.new_sign_count, v.credential_backed_up, parsed.raw_id),
        )
    return email
