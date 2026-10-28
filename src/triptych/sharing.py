"""Sharing a trip, with other Triptych users by invite and with anyone by link.

An invite names an email address and a role (models.Role). It waits, matched by address, until
someone with that address verified on their account (senders.py) accepts it; until then it shows
nothing. Viewers see the trip without confirmation codes; editors and the owner see everything (but
where it came from).

Anyone with a link sees the trip read-only, without what could change a booking (codes, seats,
travellers, notes) or say where it came from.

A link's token is derived from its id (an HMAC under SECRET_KEY), so its owner can copy it again,
and only its SHA-256 is stored, so the database alone can't open it. Changing SECRET_KEY breaks
every link, as it does the calendar feeds. Deleting a link's row revokes it."""

import base64
import hashlib
import hmac
import uuid
from datetime import date, datetime, timedelta

from pydantic import BaseModel

from triptych import db
from triptych.config import settings
from triptych.models import Reservation, Role, Trip

SHARED_FIELDS = frozenset(
    {
        "id", "kind", "title", "provider", "start_local", "start_tz", "end_local", "end_tz",
        "start_location", "end_location", "address", "city", "country_code", "service_number",
    }
)  # fmt: skip
"""What a link shows of each reservation. A field not listed here keeps its default, so a field
added to Reservation stays hidden from links until it's added on purpose."""


class ShareLink(BaseModel):
    id: str
    label: str | None
    created_at: datetime
    expires_at: datetime | None
    last_used_at: datetime | None
    token: str

    @property
    def last_day(self) -> date | None:
        """The last day (UTC) it works, if it expires."""
        return (self.expires_at - timedelta(microseconds=1)).date() if self.expires_at else None


class Member(BaseModel):
    email: str
    role: Role
    accepted: bool


class Invite(BaseModel):
    trip_id: str
    trip_name: str
    role: Role
    invited_by: str | None
    """The owner's address."""


class LinkedTrip(BaseModel):
    trip_id: str
    owner: str


def token_for(link_id: str) -> str:
    mac = hmac.new(settings.secret_key.encode(), f"share-link:{link_id}".encode(), hashlib.sha256).digest()
    return base64.urlsafe_b64encode(mac).rstrip(b"=").decode()


def _hash(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()


EXPIRES_AFTER_TRIP = timedelta(days=7)
"""How long after its trip's last day a link made to expire keeps working."""


async def create_link(
    owner: str, trip_id: str, label: str | None, expire_after_trip: bool = False
) -> str | None:
    """A new link to the owner's trip (None if it isn't theirs). Returns its id. One made to expire
    stops at the end of the day EXPIRES_AFTER_TRIP after the trip's last day as it is now (UTC)."""
    link_id = str(uuid.uuid4())
    async with db.connection() as conn:
        cur = await conn.execute(
            """INSERT INTO share_link (id, trip_id, token_sha256, label, expires_at)
               SELECT %(id)s, t.id, %(hash)s, %(label)s,
                      CASE WHEN %(expire)s THEN
                        ((SELECT max(coalesce(r.end_local, r.start_local))::date FROM reservation r
                          WHERE r.trip_id = t.id) + %(after)s + 1)::timestamp AT TIME ZONE 'UTC'
                      END
               FROM trip t WHERE t.id = %(trip)s AND t.owner = %(owner)s RETURNING id""",
            {
                "id": link_id,
                "hash": _hash(token_for(link_id)),
                "label": (label or "").strip() or None,
                "expire": expire_after_trip,
                "after": EXPIRES_AFTER_TRIP.days,
                "trip": trip_id,
                "owner": owner,
            },
        )
        return link_id if await cur.fetchone() else None


async def links(owner: str, trip_id: str) -> list[ShareLink]:
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT l.id::text, l.label, l.created_at, l.expires_at, l.last_used_at
               FROM share_link l JOIN trip t ON t.id = l.trip_id
               WHERE t.id = %s AND t.owner = %s AND (l.expires_at IS NULL OR l.expires_at > now())
               ORDER BY l.created_at""",
            (trip_id, owner),
        )
        rows = await cur.fetchall()
    return [
        ShareLink(id=i, label=label, created_at=c, expires_at=e, last_used_at=u, token=token_for(i))
        for i, label, c, e, u in rows
    ]


async def revoke_link(owner: str, trip_id: str, link_id: str) -> None:
    try:
        uuid.UUID(link_id)
    except ValueError:
        return
    async with db.connection() as conn:
        await conn.execute(
            """DELETE FROM share_link l USING trip t
               WHERE l.id = %s AND l.trip_id = t.id AND t.id = %s AND t.owner = %s""",
            (link_id, trip_id, owner),
        )


async def open_link(token: str) -> LinkedTrip | None:
    """The trip a live link shows, noting that it was used. None for a revoked or expired one."""
    async with db.connection() as conn:
        cur = await conn.execute(
            """UPDATE share_link l SET last_used_at = now() FROM trip t
               WHERE l.token_sha256 = %s AND t.id = l.trip_id
                 AND (l.expires_at IS NULL OR l.expires_at > now())
               RETURNING t.id::text, t.owner::text""",
            (_hash(token),),
        )
        row = await cur.fetchone()
    return LinkedTrip(trip_id=row[0], owner=row[1]) if row else None


async def delete_expired_links() -> int:
    """Drop links that have expired (they already open nothing). Returns how many."""
    async with db.connection() as conn:
        cur = await conn.execute("DELETE FROM share_link WHERE expires_at <= now()")
        return cur.rowcount


# --- invites ---------------------------------------------------------------------------------


async def invite(owner: str, trip_id: str, email: str, role: Role = Role.VIEW) -> None:
    """Invite whoever has `email` to the owner's trip. Inviting the same address again changes its
    role. Says nothing about whether the address has an account, so it can't be used to find out."""
    email = db.normalise_email(email)
    if "@" not in email or role is Role.OWNER:
        return
    async with db.connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT user_id::text FROM user_email WHERE email = %s", (email,))
        row = await cur.fetchone()
        invitee = row[0] if row else None
        if invitee == owner:
            return
        await conn.execute(
            """INSERT INTO trip_member (trip_id, email, role, invited_by)
               SELECT id, %(email)s, %(role)s, owner FROM trip WHERE id = %(trip)s AND owner = %(owner)s
               ON CONFLICT (trip_id, email) DO UPDATE SET role = EXCLUDED.role""",
            {"email": email, "role": role.value, "trip": trip_id, "owner": owner},
        )
        if invitee:
            await db.notify_changed(conn, invitee)


async def members(owner: str, trip_id: str) -> list[Member]:
    async with db.connection() as conn:
        cur = await conn.execute(
            """SELECT m.email, m.role, m.user_id IS NOT NULL
               FROM trip_member m JOIN trip t ON t.id = m.trip_id
               WHERE t.id = %s AND t.owner = %s ORDER BY m.created_at""",
            (trip_id, owner),
        )
        rows = await cur.fetchall()
    return [Member(email=e, role=Role(r), accepted=a) for e, r, a in rows]


async def remove_member(owner: str, trip_id: str, email: str) -> None:
    async with db.connection() as conn, conn.transaction():
        cur = await conn.execute(
            """DELETE FROM trip_member m USING trip t
               WHERE m.trip_id = t.id AND t.id = %s AND t.owner = %s AND m.email = %s
               RETURNING m.user_id::text""",
            (trip_id, owner, db.normalise_email(email)),
        )
        row = await cur.fetchone()
        if row and row[0]:
            await db.notify_changed(conn, row[0])


# An invite reaches a user through any of their verified addresses (senders.py), until they accept it.
MINE = """m.user_id IS NULL AND m.email IN (SELECT email FROM user_email
                                         WHERE user_id = %(me)s AND verified_at IS NOT NULL)"""


async def pending_invites(user_id: str) -> list[Invite]:
    async with db.connection() as conn:
        cur = await conn.execute(
            f"""SELECT DISTINCT ON (t.id) t.id::text, t.name, m.role,
                       (SELECT e.email FROM user_email e WHERE e.user_id = t.owner
                        ORDER BY e.created_at, e.email LIMIT 1)
                FROM trip_member m JOIN trip t ON t.id = m.trip_id
                WHERE {MINE} AND t.owner <> %(me)s
                  AND NOT EXISTS (SELECT 1 FROM trip_member a WHERE a.trip_id = t.id AND a.user_id = %(me)s)
                ORDER BY t.id, m.created_at""",
            {"me": user_id},
        )
        rows = await cur.fetchall()
    return [Invite(trip_id=i, trip_name=n, role=Role(r), invited_by=by) for i, n, r, by in rows]


async def accept(user_id: str, trip_id: str) -> bool:
    """Accept an invite to the trip sent to one of the user's addresses. False if there's none."""
    async with db.connection() as conn, conn.transaction():
        cur = await conn.execute(
            f"""UPDATE trip_member SET user_id = %(me)s, accepted_at = now()
                WHERE ctid = (SELECT m.ctid FROM trip_member m JOIN trip t ON t.id = m.trip_id
                              WHERE m.trip_id = %(trip)s AND {MINE} AND t.owner <> %(me)s
                                AND NOT EXISTS (SELECT 1 FROM trip_member a
                                                WHERE a.trip_id = m.trip_id AND a.user_id = %(me)s)
                              LIMIT 1)
                RETURNING 1""",
            {"me": user_id, "trip": trip_id},
        )
        if await cur.fetchone() is None:
            return False
        await db.notify_changed(conn, user_id)
        return True


async def decline(user_id: str, trip_id: str) -> None:
    async with db.connection() as conn, conn.transaction():
        await conn.execute(
            f"DELETE FROM trip_member m WHERE m.trip_id = %(trip)s AND {MINE}",
            {"me": user_id, "trip": trip_id},
        )
        await db.notify_changed(conn, user_id)


async def leave(user_id: str, trip_id: str) -> None:
    async with db.connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM trip_member WHERE trip_id = %s AND user_id = %s", (trip_id, user_id))
        await db.notify_changed(conn, user_id)


async def in_feed(user_id: str, trip_id: str) -> bool:
    """Whether a trip shared with the user is in their calendar feed."""
    async with db.connection() as conn:
        cur = await conn.execute(
            "SELECT in_feed FROM trip_member WHERE trip_id = %s AND user_id = %s", (trip_id, user_id)
        )
        row = await cur.fetchone()
    return bool(row and row[0])


async def set_in_feed(user_id: str, trip_id: str, shown: bool) -> None:
    async with db.connection() as conn:
        await conn.execute(
            "UPDATE trip_member SET in_feed = %s WHERE trip_id = %s AND user_id = %s",
            (shown, trip_id, user_id),
        )


VIEWER_HIDDEN = frozenset({"confirmation_code", "source_ingest_id", "source_run_id"})
"""What a viewer doesn't see of each reservation: a code is enough to change or cancel a booking."""


def viewer_view(trip: Trip) -> Trip:
    """The trip as an invited viewer sees it."""
    return trip.model_copy(
        update={
            "reservations": [
                Reservation.model_validate(r.model_dump(exclude=set(VIEWER_HIDDEN)))
                for r in trip.reservations
            ]
        }
    )


def shared_view(trip: Trip) -> Trip:
    """The trip as a link shows it: each reservation with only SHARED_FIELDS."""
    return Trip(
        id=trip.id,
        owner="",
        name=trip.name,
        destination=trip.destination,
        photo=trip.photo,
        reservations=[
            Reservation.model_validate({k: v for k, v in r.model_dump().items() if k in SHARED_FIELDS})
            for r in trip.reservations
        ],
    )
