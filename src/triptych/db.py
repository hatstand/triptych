"""Postgres access: plain SQL (psycopg 3, async) mapped onto the pydantic models.

Postgres is the system of record for trips, reservations, inbox items and notifications. Restate
handlers call the write functions from inside `ctx.run`, so each one must be safe to re-run:
writes are upserts keyed by ids that Restate generated deterministically, and `file_reservations`
detects that it has already been applied.
"""

import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from datetime import datetime

import restate
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import class_row
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel

from triptych import enrich, grouping
from triptych.config import settings
from triptych.models import InboxItem, Notification, Reservation, Trip, TripSummary

_pool: AsyncConnectionPool | None = None


def init(url: str | None = None) -> None:
    """Point the module at a database (tests use a throwaway one). Opened lazily on first use."""
    global _pool
    _pool = AsyncConnectionPool(url or settings.database_url, min_size=1, max_size=5, open=False)


@asynccontextmanager
async def connection() -> AsyncGenerator[AsyncConnection]:
    if _pool is None:
        init()
    assert _pool is not None
    if _pool.closed:
        await _pool.open(wait=True)
    async with _pool.connection() as conn:
        yield conn


async def close() -> None:
    if _pool is not None and not _pool.closed:
        await _pool.close()


# --- shared SQL ------------------------------------------------------------------------------

RESERVATION_COLUMNS = """
    id::text, kind, title, provider, confirmation_code, start_local, start_tz, end_local, end_tz,
    start_location, end_location, address, city, flight_number, seat, travelers, notes,
    source_ingest_id
"""

SUMMARY_SQL = """
    SELECT t.id::text AS id, t.name, t.destination,
           min(r.start_local)::date AS start_date,
           max(coalesce(r.end_local, r.start_local))::date AS end_date,
           count(r.id)::int AS reservation_count
    FROM trip t JOIN reservation r ON r.trip_id = t.id
"""


def dedupe_key(r: Reservation) -> str:
    """Same segment re-sent (or updated) → same key, so it replaces rather than duplicates."""
    return "|".join(
        (r.kind.value, r.confirmation_code or "", r.flight_number or r.title, r.start_date.isoformat())
    )


def new_trip_id(ingest_id: str, n: int) -> str:
    """Deterministic, so a retried filing step creates the same trips."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"triptych:{ingest_id}/{n}"))


async def _summaries(conn: AsyncConnection, owner: str) -> list[TripSummary]:
    async with conn.cursor(row_factory=class_row(TripSummary)) as cur:
        await cur.execute(SUMMARY_SQL + " WHERE t.owner = %s GROUP BY t.id ORDER BY start_date", (owner,))
        return await cur.fetchall()


async def _reservations(conn: AsyncConnection, trip_ids: list[str]) -> list[Reservation]:
    async with conn.cursor(row_factory=class_row(Reservation)) as cur:
        await cur.execute(
            f"""SELECT {RESERVATION_COLUMNS} FROM reservation
                WHERE trip_id = ANY(%s::uuid[]) ORDER BY start_local""",
            (trip_ids,),
        )
        return await cur.fetchall()


async def _upsert_reservation(conn: AsyncConnection, trip_id: str, r: Reservation) -> None:
    await conn.execute(
        """
        INSERT INTO reservation (id, trip_id, kind, title, provider, confirmation_code, start_local,
            start_tz, end_local, end_tz, start_location, end_location, address, city, flight_number,
            seat, travelers, notes, source_ingest_id, dedupe_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (trip_id, dedupe_key) DO UPDATE SET
            -- keep the existing id: it keys this segment's scheduled reminders (see reminders.py)
            title = EXCLUDED.title, provider = EXCLUDED.provider,
            confirmation_code = EXCLUDED.confirmation_code, start_local = EXCLUDED.start_local,
            start_tz = EXCLUDED.start_tz, end_local = EXCLUDED.end_local, end_tz = EXCLUDED.end_tz,
            start_location = EXCLUDED.start_location, end_location = EXCLUDED.end_location,
            address = EXCLUDED.address, city = EXCLUDED.city, flight_number = EXCLUDED.flight_number,
            seat = EXCLUDED.seat, travelers = EXCLUDED.travelers, notes = EXCLUDED.notes,
            source_ingest_id = EXCLUDED.source_ingest_id
        """,
        (
            r.id, trip_id, r.kind.value, r.title, r.provider, r.confirmation_code, r.start_local,
            r.start_tz, r.end_local, r.end_tz, r.start_location, r.end_location, r.address, r.city,
            r.flight_number, r.seat, r.travelers, r.notes, r.source_ingest_id, dedupe_key(r),
        ),
    )  # fmt: skip


async def _merge_trip(conn: AsyncConnection, absorbed: str, into: str) -> None:
    # Segments already present in the surviving trip win; move the rest, then repoint references.
    await conn.execute(
        """DELETE FROM reservation a WHERE a.trip_id = %s AND EXISTS (
               SELECT 1 FROM reservation b WHERE b.trip_id = %s AND b.dedupe_key = a.dedupe_key)""",
        (absorbed, into),
    )
    await conn.execute("UPDATE reservation SET trip_id = %s WHERE trip_id = %s", (into, absorbed))
    await conn.execute("UPDATE notification SET trip_id = %s WHERE trip_id = %s", (into, absorbed))
    await conn.execute(
        """UPDATE inbox_item SET trip_ids = array_replace(trip_ids, %s::uuid, %s::uuid)
           WHERE %s::uuid = ANY(trip_ids)""",
        (absorbed, into, absorbed),
    )
    await conn.execute("DELETE FROM trip WHERE id = %s", (absorbed,))


async def _fill_timezones(conn: AsyncConnection, trip_id: str) -> None:
    rs = await _reservations(conn, [trip_id])
    before = [(r.start_tz, r.end_tz) for r in rs]
    enrich.fill_timezones(rs)
    for r, old in zip(rs, before, strict=True):
        if (r.start_tz, r.end_tz) != old:
            await conn.execute(
                "UPDATE reservation SET start_tz = %s, end_tz = %s WHERE id = %s",
                (r.start_tz, r.end_tz, r.id),
            )


# --- users -----------------------------------------------------------------------------------
# A user is an app_user id. Email addresses (and, later, other identities) point at it; everything
# a user owns is keyed by the id, which is what `owner` means throughout.


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def user_for_email(email: str) -> str:
    """The user an address belongs to. A new address gets a new user: mail can arrive before its
    owner has signed up, and invites can name an address that has never sent any."""
    email = normalise_email(email)
    while True:
        async with connection() as conn:
            cur = await conn.execute("SELECT user_id::text FROM user_email WHERE email = %s", (email,))
            if row := await cur.fetchone():
                return row[0]
            try:
                async with conn.transaction():
                    user_id = str(uuid.uuid4())
                    await conn.execute("INSERT INTO app_user (id) VALUES (%s)", (user_id,))
                    await conn.execute(
                        "INSERT INTO user_email (email, user_id) VALUES (%s, %s)", (email, user_id)
                    )
                    return user_id
            except UniqueViolation:
                continue  # someone else claimed the address first; use theirs


async def primary_email(user_id: str) -> str | None:
    """The address to show for a user: their first one."""
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT email FROM user_email WHERE user_id = %s ORDER BY created_at, email LIMIT 1", (user_id,)
        )
        row = await cur.fetchone()
    return row[0] if row else None


# --- writes (called from Restate handlers inside ctx.run) -------------------------------------


class FiledReservation(BaseModel):
    trip_id: str
    reservation: Reservation


class Filed(BaseModel):
    trip_ids: list[str]
    reservations: list[FiledReservation]
    """Every reservation in the touched trips, as stored. Not just the new ones: filing can fill in
    the timezone of an earlier segment (hotel first, flight later), making its reminder possible."""


async def file_reservations(owner: str, ingest_id: str, reservations: list[Reservation]) -> Filed:
    ids = [r.id for r in reservations]
    async with connection() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM trip WHERE owner = %s FOR UPDATE", (owner,))

        cur = await conn.execute(
            "SELECT DISTINCT trip_id::text FROM reservation WHERE id = ANY(%s::uuid[])", (ids,)
        )
        already = [row[0] for row in await cur.fetchall()]
        if not already:
            counter = iter(range(len(reservations) + 1))
            buckets = grouping.assign(
                reservations, await _summaries(conn, owner), lambda: new_trip_id(ingest_id, next(counter))
            )
            for b in buckets:
                for absorbed in b.absorbed:
                    await _merge_trip(conn, absorbed, b.trip_id)
                if b.is_new:
                    name = grouping.trip_name(b.destination, b.start)
                    await conn.execute(
                        "INSERT INTO trip (id, owner, name, destination) VALUES (%s, %s, %s, %s)",
                        (b.trip_id, owner, name, b.destination),
                    )
                for r in b.reservations:
                    await _upsert_reservation(conn, b.trip_id, r)
                await _fill_timezones(conn, b.trip_id)
            already = [b.trip_id for b in buckets]

        stored = [
            FiledReservation(trip_id=tid, reservation=r)
            for tid in already
            for r in await _reservations(conn, [tid])
        ]
        return Filed(trip_ids=already, reservations=stored)


async def _owned(conn: AsyncConnection, owner: str, trip_id: str) -> None:
    cur = await conn.execute("SELECT 1 FROM trip WHERE id = %s AND owner = %s FOR UPDATE", (trip_id, owner))
    if await cur.fetchone() is None:
        raise restate.TerminalError("trip not found", status_code=404)


async def remove_reservation(owner: str, trip_id: str, reservation_id: str) -> bool:
    """Returns whether the trip still exists (it's deleted along with its last reservation)."""
    async with connection() as conn, conn.transaction():
        await _owned(conn, owner, trip_id)
        await conn.execute(
            "DELETE FROM reservation WHERE id = %s AND trip_id = %s", (reservation_id, trip_id)
        )
        cur = await conn.execute("SELECT 1 FROM reservation WHERE trip_id = %s LIMIT 1", (trip_id,))
        if await cur.fetchone() is None:
            await conn.execute("DELETE FROM trip WHERE id = %s", (trip_id,))
            return False
        return True


async def rename_trip(owner: str, trip_id: str, name: str) -> None:
    async with connection() as conn, conn.transaction():
        await _owned(conn, owner, trip_id)
        if name.strip():
            await conn.execute("UPDATE trip SET name = %s WHERE id = %s", (name.strip(), trip_id))


async def delete_trip(owner: str, trip_id: str) -> None:
    async with connection() as conn:
        await conn.execute("DELETE FROM trip WHERE id = %s AND owner = %s", (trip_id, owner))


async def upsert_inbox(owner: str, item: InboxItem) -> None:
    async with connection() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO inbox_item (ingest_id, owner, source, label, received_at, state, message, trip_ids)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid[])
            ON CONFLICT (ingest_id) DO UPDATE SET label = EXCLUDED.label, state = EXCLUDED.state,
                message = EXCLUDED.message, trip_ids = EXCLUDED.trip_ids
            """,
            (item.ingest_id, owner, item.source, item.label, item.received_at, item.state, item.message,
             item.trip_ids),
        )  # fmt: skip


async def add_notification(owner: str, n: Notification) -> None:
    async with connection() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO notification (id, owner, at, trip_id, text) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (n.id, owner, n.at, n.trip_id, n.text),
        )


async def dismiss_notifications(owner: str) -> None:
    async with connection() as conn:
        await conn.execute("DELETE FROM notification WHERE owner = %s", (owner,))


class ReminderTarget(BaseModel):
    owner: str | None = None
    trip_id: str | None = None


async def reminder_target(reservation_id: str, expected_start: datetime) -> ReminderTarget:
    """Who to notify, or empty if the segment was deleted or rescheduled since we scheduled it."""
    async with connection() as conn:
        cur = await conn.execute(
            """SELECT t.owner::text, t.id::text FROM reservation r JOIN trip t ON t.id = r.trip_id
               WHERE r.id = %s AND r.start_local = %s""",
            (reservation_id, expected_start),
        )
        row = await cur.fetchone()
    return ReminderTarget(owner=row[0], trip_id=row[1]) if row else ReminderTarget()


# --- reads (web app, assistant) ---------------------------------------------------------------


async def trip_summaries(owner: str) -> list[TripSummary]:
    async with connection() as conn:
        return await _summaries(conn, owner)


async def get_trip(owner: str, trip_id: str) -> Trip | None:
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT name, destination FROM trip WHERE id = %s AND owner = %s", (trip_id, owner)
        )
        row = await cur.fetchone()
        if row is None:
            return None
        reservations = await _reservations(conn, [trip_id])
    return Trip(id=trip_id, owner=owner, name=row[0], destination=row[1], reservations=reservations)


async def all_trips(owner: str) -> list[Trip]:
    """Every trip with its reservations in two queries (the calendar feed)."""
    async with connection() as conn:
        cur = await conn.execute("SELECT id::text, name, destination FROM trip WHERE owner = %s", (owner,))
        trips = {
            tid: Trip(id=tid, owner=owner, name=name, destination=dest)
            for tid, name, dest in await cur.fetchall()
        }
        cur = await conn.execute(
            f"""SELECT r.trip_id::text, {RESERVATION_COLUMNS.replace("id::text", "r.id::text", 1)}
                FROM reservation r JOIN trip t ON t.id = r.trip_id
                WHERE t.owner = %s ORDER BY r.start_local""",
            (owner,),
        )
        names = [d.name for d in cur.description or []][1:]
        for row in await cur.fetchall():
            trips[row[0]].reservations.append(Reservation(**dict(zip(names, row[1:], strict=True))))
    return list(trips.values())


async def inbox(owner: str, limit: int = 10) -> list[InboxItem]:
    async with connection() as conn, conn.cursor(row_factory=class_row(InboxItem)) as cur:
        await cur.execute(
            """SELECT ingest_id, source, label, received_at, state, message,
                      ARRAY(SELECT x::text FROM unnest(trip_ids) x) AS trip_ids
               FROM inbox_item WHERE owner = %s ORDER BY received_at DESC LIMIT %s""",
            (owner, limit),
        )
        return await cur.fetchall()


async def notifications(owner: str) -> list[Notification]:
    async with connection() as conn, conn.cursor(row_factory=class_row(Notification)) as cur:
        await cur.execute(
            """SELECT id::text, at, trip_id::text, text FROM notification
               WHERE owner = %s ORDER BY at DESC LIMIT 50""",
            (owner,),
        )
        return await cur.fetchall()


# --- admin (every user's data; only the Tailscale-gated admin app calls these) ----------------


class AdminInboxItem(InboxItem):
    owner: str
    """The owner's primary address (their user id if they have none)."""


async def ingest_counts(since: datetime) -> dict[str, int]:
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT state, count(*)::int FROM inbox_item WHERE received_at >= %s GROUP BY state", (since,)
        )
        return {state: n for state, n in await cur.fetchall()}


async def recent_ingests(state: str | None = None, limit: int = 100) -> list[AdminInboxItem]:
    async with connection() as conn, conn.cursor(row_factory=class_row(AdminInboxItem)) as cur:
        await cur.execute(
            """SELECT ingest_id, source, label, received_at, state, message,
                      ARRAY(SELECT x::text FROM unnest(trip_ids) x) AS trip_ids,
                      coalesce((SELECT email FROM user_email e WHERE e.user_id = i.owner
                                ORDER BY created_at, email LIMIT 1), owner::text) AS owner
               FROM inbox_item i WHERE %(state)s::text IS NULL OR state = %(state)s
               ORDER BY received_at DESC LIMIT %(limit)s""",
            {"state": state, "limit": limit},
        )
        return await cur.fetchall()
