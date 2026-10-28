"""Verified sender addresses: which addresses may file mail, and for whom.

Mail to plans@ is filed under the user whose address (a user_email row) it comes from, and refused
from any address no user has. An address gets there by being named on the invite its user signed
up with, or by the user adding it on the account page: they're shown a code, and mail from that
address carrying the code (authenticated like any other inbound mail, see inbound.py) proves they
control it. The code is bound to the address it was issued for, so it can't vouch for another.

An address is verified once mail authenticated by DKIM as coming from it has reached us: a code does
that, and so does the first booking forwarded from an address an admin invite named. Only verified
addresses match trip invites (sharing.py), since an unproven one may not be its user's at all.
"""

import hashlib
import re
import secrets
from datetime import datetime, timedelta

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from pydantic import BaseModel

from triptych import db

CODE_TTL = timedelta(days=1)
# No 0/O, 1/I/L, U: easy to read off a screen and type into a subject line.
ALPHABET = "ABCDEFGHJKMNPQRSTVWXYZ23456789"
CODE = re.compile(rf"\bTRIPTYCH-([{ALPHABET}]{{10}})\b", re.IGNORECASE)


class SenderError(Exception):
    """The message is safe to show the user (or to bounce to the sender)."""


class Sender(BaseModel):
    email: str
    created_at: datetime
    verified_at: datetime | None


class PendingSender(BaseModel):
    email: str
    expires_at: datetime


def code_hash(code: str) -> bytes:
    return hashlib.sha256(code.upper().encode()).digest()


def looks_like_address(email: str) -> bool:
    return re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email) is not None


def find_code(text: str) -> str | None:
    """The verification code in a subject line or body, normalised, if there is one."""
    m = CODE.search(text)
    return f"TRIPTYCH-{m.group(1).upper()}" if m else None


async def owner(email: str) -> str | None:
    """The user mail from `email` is filed under, or None if it isn't anyone's verified address."""
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT user_id::text FROM user_email WHERE email = %s", (db.normalise_email(email),)
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def addresses(user_id: str) -> list[Sender]:
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT email, created_at, verified_at FROM user_email WHERE user_id = %s
               ORDER BY created_at, email""",
            (user_id,),
        )
        return [Sender(email=e, created_at=c, verified_at=v) for e, c, v in await cur.fetchall()]


async def pending(user_id: str) -> list[PendingSender]:
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT email, expires_at FROM sender_verification
               WHERE user_id = %s AND expires_at > now() ORDER BY created_at""",
            (user_id,),
        )
        return [PendingSender(email=e, expires_at=x) for e, x in await cur.fetchall()]


async def request(user_id: str, email: str) -> str:
    """A code that, sent from `email` to plans@, adds `email` to this user's senders. Replaces any
    earlier code for the same address."""
    email = db.normalise_email(email)
    if not looks_like_address(email):
        raise SenderError("That doesn't look like an email address.")
    taken = await owner(email)
    if taken == user_id:
        raise SenderError(f"{email} is already one of your addresses.")
    if taken is not None:
        raise SenderError(f"{email} belongs to another Triptych account.")
    code = "TRIPTYCH-" + "".join(secrets.choice(ALPHABET) for _ in range(10))
    async with db.connection() as conn, conn.transaction():
        await conn.execute(
            "DELETE FROM sender_verification WHERE expires_at <= now() OR (user_id = %s AND email = %s)",
            (user_id, email),
        )
        await conn.execute(
            """INSERT INTO sender_verification (code_hash, user_id, email, expires_at)
               VALUES (%s, %s, %s, now() + %s)""",
            (code_hash(code), user_id, email, CODE_TTL),
        )
    return code


async def is_pending(code: str, email: str) -> bool:
    """Whether `code` is live and was issued for `email` (cheap; checked before authenticating)."""
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM sender_verification WHERE code_hash = %s AND email = %s AND expires_at > now()",
            (code_hash(code), db.normalise_email(email)),
        )
        return await cur.fetchone() is not None


async def confirm(code: str, email: str) -> str:
    """Spend `code` on mail authenticated as coming from `email`: the address becomes a sender for
    the user who asked for the code, whose id is returned."""
    email = db.normalise_email(email)
    async with db.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """DELETE FROM sender_verification
               WHERE code_hash = %s AND email = %s AND expires_at > now() RETURNING user_id::text""",
            (code_hash(code), email),
        )
        row = await cur.fetchone()
        if row is None:
            raise SenderError("this verification code has expired or was issued for another address")
        user_id: str = row[0]
        cur = await conn.execute("SELECT user_id::text FROM user_email WHERE email = %s", (email,))
        if existing := await cur.fetchone():
            if existing[0] != user_id:
                raise SenderError(f"{email} already belongs to another Triptych account")
            await _mark_verified(conn, email)
            return user_id
        try:
            async with conn.transaction():
                await conn.execute(
                    "INSERT INTO user_email (email, user_id, verified_at) VALUES (%s, %s, now())",
                    (email, user_id),
                )
        except UniqueViolation as e:
            raise SenderError(f"{email} already belongs to another Triptych account") from e
    return user_id


async def _mark_verified(conn: AsyncConnection, email: str) -> None:
    await conn.execute(
        "UPDATE user_email SET verified_at = now() WHERE email = %s AND verified_at IS NULL", (email,)
    )


async def mark_verified(email: str) -> None:
    """Mail authenticated by DKIM as coming from `email` has arrived: it is proven, if it wasn't."""
    async with db.connection() as conn:
        await _mark_verified(conn, db.normalise_email(email))


async def remove(user_id: str, email: str) -> None:
    """Refuses to remove someone's last address: invites (and so passkey recovery) name one."""
    email = db.normalise_email(email)
    async with db.connection() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM app_user WHERE id = %s FOR UPDATE", (user_id,))
        cur = await conn.execute("SELECT email FROM user_email WHERE user_id = %s", (user_id,))
        mine = [row[0] for row in await cur.fetchall()]
        if email not in mine:
            await conn.execute(
                "DELETE FROM sender_verification WHERE user_id = %s AND email = %s", (user_id, email)
            )
            return
        if len(mine) == 1:
            raise SenderError("That's your only address. Add another before removing it.")
        await conn.execute("DELETE FROM user_email WHERE user_id = %s AND email = %s", (user_id, email))
