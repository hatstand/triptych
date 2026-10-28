"""Passkeys (WebAuthn), sign-in sessions and invites. Passkeys are the only way to sign in.

Users are app_user ids, with email addresses pointing at them (see db.py); those addresses are
also the only ones mail is accepted from (senders.py). Nothing here proves control of an address:
the first passkey for a user comes either from a signup whose address mail has proven (signup.py),
which creates the account with it, or from an invite naming one of their addresses
(`python -m triptych.invite`), which is also the recovery path for someone who has lost every
passkey. Further passkeys are added while signed in.

Ceremony state (challenge, purpose, user, invite) travels in a short-lived HMAC-signed cookie
rather than a table. A challenge can therefore be presented more than once within its few
minutes, but replaying it needs both that cookie and the authenticator's response from the same
ceremony; invites themselves are still spent in the database.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
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

from triptych import db, signup
from triptych.config import settings

SESSION_TTL = timedelta(days=30)
INVITE_TTL = timedelta(days=7)
CHALLENGE_TTL = timedelta(minutes=5)
RP_NAME = "Triptych"

# Every ceremony requires user verification (biometric or PIN): the passkey is the only factor.
UV = structs.UserVerificationRequirement.REQUIRED

Purpose = Literal["register", "login"]


class AuthError(Exception):
    """A ceremony or invite was refused. The message is safe to show the user."""


class Me(BaseModel):
    """Whoever is signed in."""

    id: str
    email: str


class Passkey(BaseModel):
    id: str  # base64url credential id
    name: str
    backed_up: bool
    created_at: datetime
    last_used_at: datetime | None


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


def user_handle(user_id: str) -> bytes:
    return uuid.UUID(user_id).bytes


# --- invites ---------------------------------------------------------------------------------


async def create_invite(email: str, ttl: timedelta = INVITE_TTL) -> str:
    """A one-time token that lets its holder register a passkey for the user `email` belongs to."""
    token = secrets.token_urlsafe(32)
    async with db.connection() as conn:
        await conn.execute(
            "INSERT INTO invite (token_hash, email, expires_at) VALUES (%s, %s, now() + %s)",
            (_hash(token), db.normalise_email(email), ttl),
        )
    return token


class PendingInvite(BaseModel):
    email: str
    created_at: datetime
    expires_at: datetime


async def pending_invites() -> list[PendingInvite]:
    """Unused, unexpired invites, newest first. Tokens are only stored hashed, so no links."""
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT email, created_at, expires_at FROM invite WHERE expires_at > now()
               ORDER BY created_at DESC"""
        )
        return [PendingInvite(email=e, created_at=c, expires_at=x) for e, c, x in await cur.fetchall()]


async def invite_email(token: str) -> str | None:
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT email FROM invite WHERE token_hash = %s AND expires_at > now()", (_hash(token),)
        )
        row = await cur.fetchone()
    return row[0] if row else None


# --- sessions --------------------------------------------------------------------------------


async def create_session(user_id: str) -> str:
    token = secrets.token_urlsafe(32)
    async with db.connection() as conn:
        await conn.execute("DELETE FROM web_session WHERE expires_at <= now()")
        await conn.execute(
            "INSERT INTO web_session (token_hash, user_id, expires_at) VALUES (%s, %s, now() + %s)",
            (_hash(token), user_id, SESSION_TTL),
        )
    return token


async def session_user(token: str) -> Me | None:
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT s.user_id::text,
                      (SELECT email FROM user_email e WHERE e.user_id = s.user_id
                       ORDER BY created_at, email LIMIT 1)
               FROM web_session s WHERE s.token_hash = %s AND s.expires_at > now()""",
            (_hash(token),),
        )
        row = await cur.fetchone()
    return Me(id=row[0], email=row[1] or "") if row else None


async def end_session(token: str) -> None:
    async with db.connection() as conn:
        await conn.execute("DELETE FROM web_session WHERE token_hash = %s", (_hash(token),))


# --- passkey management ----------------------------------------------------------------------


async def passkeys(user_id: str) -> list[Passkey]:
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT credential_id, name, backed_up, created_at, last_used_at FROM passkey
               WHERE user_id = %s ORDER BY created_at""",
            (user_id,),
        )
        return [
            Passkey(id=bytes_to_base64url(cid), name=name, backed_up=b, created_at=c, last_used_at=u)
            for cid, name, b, c, u in await cur.fetchall()
        ]


async def delete_passkey(user_id: str, passkey_id: str) -> None:
    """Refuses to remove someone's last passkey, which would lock them out."""
    async with db.connection() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM app_user WHERE id = %s FOR UPDATE", (user_id,))
        cur = await conn.execute("SELECT credential_id FROM passkey WHERE user_id = %s", (user_id,))
        ids = [bytes_to_base64url(row[0]) for row in await cur.fetchall()]
        if passkey_id not in ids:
            raise AuthError("No such passkey.")
        if len(ids) == 1:
            raise AuthError("That's your only passkey. Add another before removing it.")
        await conn.execute(
            "DELETE FROM passkey WHERE user_id = %s AND credential_id = %s",
            (user_id, webauthn.base64url_to_bytes(passkey_id)),
        )


# --- ceremony state (signed, so the server keeps none) ---------------------------------------


class Ceremony(BaseModel):
    purpose: Purpose
    challenge: str  # base64url
    expires: float  # unix time
    user_id: str | None = None  # registration: who the passkey is for
    invite_hash: str | None = None  # registration from an invite: hex sha256 of its token
    signup_hash: str | None = (
        None  # registration that creates its account (signup.py): hex sha256 of its token
    )


def _sign(payload: str) -> str:
    return hmac.new(settings.secret_key.encode(), f"webauthn:{payload}".encode(), hashlib.sha256).hexdigest()


def seal(c: Ceremony) -> str:
    payload = base64.urlsafe_b64encode(c.model_dump_json().encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload)}"


def unseal(token: str | None, purpose: Purpose) -> Ceremony:
    payload, _, sig = (token or "").partition(".")
    if not payload or not hmac.compare_digest(sig, _sign(payload)):
        raise AuthError("Sign-in expired. Please try again.")
    c = Ceremony.model_validate(json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4))))
    if c.purpose != purpose or c.expires < time.time():
        raise AuthError("Sign-in expired. Please try again.")
    return c


def _ceremony(purpose: Purpose, challenge: bytes, **kw: Any) -> str:
    expires = time.time() + CHALLENGE_TTL.total_seconds()
    return seal(Ceremony(purpose=purpose, challenge=bytes_to_base64url(challenge), expires=expires, **kw))


# --- ceremonies ------------------------------------------------------------------------------


async def start_registration(
    user_id: str, email: str, invite: str | None = None, signup_token: str | None = None
) -> tuple[str, str]:
    """(sealed ceremony state, PublicKeyCredentialCreationOptions JSON). Without an invite or a
    signup, `user_id` must be whoever is signed in; with a signup, it's the id the account will get."""
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT credential_id, transports FROM passkey WHERE user_id = %s", (user_id,)
        )
        existing = await cur.fetchall()
    options = webauthn.generate_registration_options(
        rp_id=settings.webauthn_rp_id,
        rp_name=RP_NAME,
        user_id=user_handle(user_id),
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
    state = _ceremony(
        "register",
        options.challenge,
        user_id=user_id,
        invite_hash=_hash(invite).hex() if invite else None,
        signup_hash=signup.token_hash(signup_token).hex() if signup_token else None,
    )
    return state, webauthn.options_to_json(options)


async def finish_registration(state: str | None, credential: dict[str, Any], name: str) -> str:
    """Stores the new passkey and returns its user."""
    c = unseal(state, "register")
    assert c.user_id is not None
    try:
        parsed = parse_registration_credential_json(credential)
        v = webauthn.verify_registration_response(
            credential=parsed,
            expected_challenge=webauthn.base64url_to_bytes(c.challenge),
            expected_rp_id=settings.webauthn_rp_id,
            expected_origin=list(settings.allowed_origins),
            require_user_verification=True,
        )
    except WebAuthnException as e:
        raise AuthError(f"That passkey couldn't be registered ({e}).") from e

    transports = [t.value for t in parsed.response.transports or []]
    async with db.connection() as conn, conn.transaction():
        if c.invite_hash is not None:
            # Spend the invite, and check it still names one of this user's addresses.
            cur = await conn.execute(
                """DELETE FROM invite i USING user_email e
                   WHERE i.token_hash = %s AND i.expires_at > now() AND e.email = i.email AND e.user_id = %s
                   RETURNING 1""",
                (bytes.fromhex(c.invite_hash), c.user_id),
            )
            if await cur.fetchone() is None:
                raise AuthError("This invite has expired or was already used.")
        if c.signup_hash is not None:
            try:
                await signup.create_account(conn, bytes.fromhex(c.signup_hash), c.user_id)
            except signup.SignupError as e:
                raise AuthError(str(e)) from e
        await conn.execute(
            """INSERT INTO passkey
                   (credential_id, user_id, public_key, sign_count, transports, backed_up, name)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (credential_id) DO NOTHING""",
            (v.credential_id, c.user_id, v.credential_public_key, v.sign_count, transports,
             v.credential_backed_up, name.strip()[:60] or "Passkey"),
        )  # fmt: skip
    return c.user_id


def start_login() -> tuple[str, str]:
    """Discoverable-credential sign-in: the browser offers whichever passkeys it has for us."""
    options = webauthn.generate_authentication_options(rp_id=settings.webauthn_rp_id, user_verification=UV)
    return _ceremony("login", options.challenge), webauthn.options_to_json(options)


async def finish_login(state: str | None, credential: dict[str, Any]) -> str:
    """Verifies the assertion and returns who signed in."""
    c = unseal(state, "login")
    try:
        parsed = parse_authentication_credential_json(credential)
    except WebAuthnException as e:
        raise AuthError("That passkey response was malformed.") from e
    async with db.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """SELECT user_id::text, public_key, sign_count FROM passkey
               WHERE credential_id = %s FOR UPDATE""",
            (parsed.raw_id,),
        )
        row = await cur.fetchone()
        if row is None:
            raise AuthError("That passkey isn't registered here. It may have been removed.")
        user_id, public_key, sign_count = row
        if parsed.response.user_handle is not None and parsed.response.user_handle != user_handle(user_id):
            raise AuthError("That passkey doesn't match its account.")
        try:
            v = webauthn.verify_authentication_response(
                credential=parsed,
                expected_challenge=webauthn.base64url_to_bytes(c.challenge),
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
    return user_id
