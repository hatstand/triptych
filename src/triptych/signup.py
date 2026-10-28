"""Signing up: proving an email address before any account exists for it.

Someone enters their address at /signup. Their browser gets a token (a cookie), and the page shows a
code derived from it, which they email to plans@ from that address. Mail that carries the code and is
authenticated by DKIM as coming from the address (inbound.py) marks the signup verified, and only
the browser holding the token can go on to make the account, together with its first passkey
(auth.finish_registration). Nothing about the account exists until then, so an abandoned signup
leaves nothing behind but a row that expires. An address that already has an account never gets
another: its owner is told to sign in (a lost passkey is recovered by an admin invite).

With SIGNUPS=waitlist a new account waits for an admin to approve it on /admin: its owner can sign
in, but sees only that they're waiting, and mail from them is refused. There's no outbound mail to
say when they're in; they find out at their next visit. Someone a trip has already been shared with
skips the queue.

The code is an HMAC of the token under SECRET_KEY, in the same form as sender codes (senders.py), so
the page can show it again on reload while the database stores only hashes of either.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from pydantic import BaseModel

from triptych import db, senders
from triptych.config import settings

TTL = timedelta(minutes=30)


class SignupError(Exception):
    """The message is safe to show the user."""


class Signup(BaseModel):
    id: str
    email: str
    code: str
    expires_at: datetime
    verified: bool
    taken: bool
    """The address already belongs to an account, whose owner should sign in instead."""

    @property
    def channel(self) -> str:
        """What the live hub (web/live.py) calls this signup's updates."""
        return channel(self.id)


def channel(signup_id: str) -> str:
    return f"signup:{signup_id}"


def token_hash(value: str) -> bytes:
    return hashlib.sha256(value.encode()).digest()


def code_for(token: str) -> str:
    digest = hmac.new(settings.secret_key.encode(), b"signup:" + token.encode(), hashlib.sha256).digest()
    n, chars = int.from_bytes(digest), []
    for _ in range(10):
        n, i = divmod(n, len(senders.ALPHABET))
        chars.append(senders.ALPHABET[i])
    return "TRIPTYCH-" + "".join(chars)


async def start(email: str) -> str:
    """A token for the browser signing up as `email`. Replaces any earlier signup for the address. Says
    nothing about whether the address already has an account, so it can't be used to find out."""
    email = db.normalise_email(email)
    if not senders.looks_like_address(email):
        raise SignupError("That doesn't look like an email address.")
    token = secrets.token_urlsafe(32)
    async with db.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM signup WHERE expires_at <= now() OR email = %s", (email,))
        await conn.execute(
            """INSERT INTO signup (token_hash, code_hash, email, expires_at)
               VALUES (%s, %s, %s, now() + %s)""",
            (token_hash(token), senders.code_hash(code_for(token)), email, TTL),
        )
    return token


async def get(token: str) -> Signup | None:
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT id::text, email, expires_at, verified_at IS NOT NULL,
                      EXISTS (SELECT 1 FROM user_email e WHERE e.email = s.email)
               FROM signup s WHERE token_hash = %s AND expires_at > now()""",
            (token_hash(token),),
        )
        row = await cur.fetchone()
    if row is None:
        return None
    signup_id, email, expires_at, verified, taken = row
    return Signup(
        id=signup_id, email=email, code=code_for(token), expires_at=expires_at, verified=verified, taken=taken
    )


async def cancel(token: str) -> None:
    async with db.connection() as conn:
        await conn.execute("DELETE FROM signup WHERE token_hash = %s", (token_hash(token),))


async def is_pending(code: str, email: str) -> bool:
    """Whether `code` is a live signup's, for `email` (cheap; checked before authenticating)."""
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT 1 FROM signup WHERE code_hash = %s AND email = %s AND expires_at > now()",
            (senders.code_hash(code), db.normalise_email(email)),
        )
        return await cur.fetchone() is not None


async def confirm(code: str, email: str) -> None:
    """Mail authenticated as coming from `email` carried `code`: that signup's address is proven."""
    async with db.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """UPDATE signup SET verified_at = coalesce(verified_at, now())
               WHERE code_hash = %s AND email = %s AND expires_at > now() RETURNING id::text""",
            (senders.code_hash(code), db.normalise_email(email)),
        )
        row = await cur.fetchone()
        if row is None:
            raise SignupError("this signup code has expired or was issued for another address")
        await db.notify_changed(conn, channel(row[0]))


async def create_account(conn: AsyncConnection, signup_token_hash: bytes, user_id: str) -> None:
    """Within the caller's transaction (auth.finish_registration, which adds the passkey): make the
    account a verified signup asked for, with its address proven, and spend the signup."""
    cur = await conn.execute(
        """DELETE FROM signup WHERE token_hash = %s AND expires_at > now() AND verified_at IS NOT NULL
           RETURNING email""",
        (signup_token_hash,),
    )
    row = await cur.fetchone()
    if row is None:
        raise SignupError("This signup has expired. Please start again.")
    # On the waitlist, unless someone has already shared a trip with the address.
    await conn.execute(
        """INSERT INTO app_user (id, waitlisted_at)
           SELECT %(id)s, CASE WHEN %(waitlist)s AND NOT EXISTS (
                      SELECT 1 FROM trip_member WHERE email = %(email)s AND user_id IS NULL)
                  THEN now() END""",
        {"id": user_id, "waitlist": settings.signups == "waitlist", "email": row[0]},
    )
    try:
        await conn.execute(
            "INSERT INTO user_email (email, user_id, verified_at) VALUES (%s, %s, now())", (row[0], user_id)
        )
    except UniqueViolation as e:  # the caller's transaction rolls back as this propagates
        raise SignupError(f"{row[0]} already has an account. Sign in instead.") from e


class Waiting(BaseModel):
    user_id: str
    email: str
    since: datetime


async def waitlist() -> list[Waiting]:
    """Accounts waiting to be let in, longest-waiting first."""
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT u.id::text, coalesce((SELECT email FROM user_email e WHERE e.user_id = u.id
                                            ORDER BY created_at, email LIMIT 1), ''), u.waitlisted_at
               FROM app_user u WHERE u.waitlisted_at IS NOT NULL ORDER BY u.waitlisted_at"""
        )
        return [Waiting(user_id=i, email=e, since=s) for i, e, s in await cur.fetchall()]


async def is_waitlisted(user_id: str) -> bool:
    async with db.connection() as conn:
        cur = await conn.execute("SELECT waitlisted_at IS NOT NULL FROM app_user WHERE id = %s", (user_id,))
        row = await cur.fetchone()
    return bool(row and row[0])


async def approve(user_id: str) -> None:
    async with db.connection() as conn:
        await conn.execute("UPDATE app_user SET waitlisted_at = NULL WHERE id = %s", (user_id,))
