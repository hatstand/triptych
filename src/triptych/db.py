"""Postgres access: plain SQL (psycopg 3, async) mapped onto the pydantic models.

Postgres is the system of record for trips, reservations, inbox items and notifications. Restate
handlers call the write functions from inside `ctx.run`, so each one must be safe to re-run:
writes are upserts keyed by ids that Restate generated deterministically, and `file_reservations`
detects that it has already been applied.

Every write also announces itself with `pg_notify` on CHANGES, naming the user whose data changed,
so the web app can push fresh pages to that user's open tabs (web/live.py). NOTIFY is delivered on
commit, so a rolled-back or retried step announces nothing it didn't do.
"""

import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta
from typing import Any, Literal, LiteralString

import restate
from psycopg import AsyncConnection
from psycopg.errors import UniqueViolation
from psycopg.rows import class_row, dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import AsyncConnectionPool
from pydantic import BaseModel, Field

from triptych import enrich, grouping
from triptych.config import settings
from triptych.models import (
    Extraction,
    ExtractionRun,
    InboxItem,
    Notification,
    Photo,
    PhotoChoice,
    RawInput,
    ReprocessRequest,
    Reservation,
    ReservationFields,
    Trip,
    TripSummary,
)

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


# --- change notifications --------------------------------------------------------------------


async def _changed(conn: AsyncConnection, owner: str) -> None:
    """Tell listeners `owner`'s trips, inbox or notifications changed, once this transaction commits."""
    await conn.execute("SELECT pg_notify('triptych_changes', %s)", (owner,))


async def changes(url: str) -> AsyncGenerator[str | None]:
    """The users whose data changed, as writes commit. Yields None first, once listening: anything
    committed before that was missed, so whoever holds stale state should refresh it. Runs on its
    own connection (a LISTEN can't share a pooled one) until that connection fails."""
    async with await AsyncConnection.connect(url, autocommit=True) as conn:
        await conn.execute("LISTEN triptych_changes")
        yield None
        async for n in conn.notifies():
            yield n.payload


# --- shared SQL ------------------------------------------------------------------------------

RESERVATION_COLUMNS = """
    id::text, kind, title, provider, confirmation_code, start_local, start_tz, end_local, end_tz,
    start_location, end_location, address, city, service_number, seat, travelers, notes,
    source_ingest_id, source_run_id::text, edited_fields
"""

# What a reservation's owner can correct on the trip page (ReservationFields).
EDITABLE = (
    "kind", "title", "provider", "confirmation_code", "start_local", "start_tz", "end_local", "end_tz",
    "start_location", "end_location", "address", "city", "service_number", "seat", "travelers", "notes",
)  # fmt: skip

# Filing a segment that is already there updates it, except for what its owner corrected. (Its
# kind is part of its dedupe key, so filing never changes that.)
UNLESS_EDITED = ",\n".join(
    f"{f} = CASE WHEN '{f}' = ANY(reservation.edited_fields) THEN reservation.{f} ELSE EXCLUDED.{f} END"
    for f in EDITABLE
    if f != "kind"
)

# A trip's photo credit (columns named photo_*), from the place_photo row for its place if that has an
# image. The version is the place and when it was fetched, so it changes with the picture. (Finding the
# subject later doesn't change it: that only moves the crop, which the page sets, not the image.)
PHOTO_COLUMNS = """
    CASE WHEN p.image IS NOT NULL THEN left(md5(p.place || p.fetched_at::text), 12) END AS photo_version,
    p.file AS photo_file, p.page_url AS photo_page_url, p.article_url AS photo_article_url,
    p.artist AS photo_artist, p.licence AS photo_licence, p.licence_url AS photo_licence_url,
    p.aspect AS photo_aspect, p.subject AS photo_subject
"""
PHOTO_JOIN = "LEFT JOIN place_photo p ON p.place = t.place"

SUMMARY_SQL = f"""
    SELECT t.id::text AS id, t.name, t.destination,
           min(r.start_local)::date AS start_date,
           max(coalesce(r.end_local, r.start_local))::date AS end_date,
           count(r.id)::int AS reservation_count,
           {PHOTO_COLUMNS}
    FROM trip t JOIN reservation r ON r.trip_id = t.id {PHOTO_JOIN}
"""


def _photo(row: dict[str, Any]) -> Photo | None:
    """Takes the photo_* columns out of `row`, as the trip's photo if it has one."""
    fields = {k.removeprefix("photo_"): row.pop(k) for k in list(row) if k.startswith("photo_")}
    return Photo.model_validate(fields) if fields["version"] else None


def dedupe_key(r: Reservation) -> str:
    """Same segment re-sent (or updated) → same key, so it replaces rather than duplicates."""
    return "|".join(
        (r.kind.value, r.confirmation_code or "", r.service_number or r.title, r.start_date.isoformat())
    )


def new_trip_id(ingest_id: str, n: int) -> str:
    """Deterministic, so a retried filing step creates the same trips."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"triptych:{ingest_id}/{n}"))


async def _summaries(conn: AsyncConnection, owner: str) -> list[TripSummary]:
    async with conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            SUMMARY_SQL + " WHERE t.owner = %s GROUP BY t.id, p.place ORDER BY start_date", (owner,)
        )
        return [TripSummary(**row, photo=_photo(row)) for row in await cur.fetchall()]


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
        f"""
        INSERT INTO reservation (id, trip_id, kind, title, provider, confirmation_code, start_local,
            start_tz, end_local, end_tz, start_location, end_location, address, city, service_number,
            seat, travelers, notes, source_ingest_id, source_run_id, dedupe_key)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (trip_id, dedupe_key) DO UPDATE SET
            -- keep the existing id: it keys this segment's scheduled reminders (see reminders.py)
            {UNLESS_EDITED},
            source_ingest_id = EXCLUDED.source_ingest_id, source_run_id = EXCLUDED.source_run_id
        """,
        (
            r.id, trip_id, r.kind.value, r.title, r.provider, r.confirmation_code, r.start_local,
            r.start_tz, r.end_local, r.end_tz, r.start_location, r.end_location, r.address, r.city,
            r.service_number, r.seat, r.travelers, r.notes, r.source_ingest_id, r.source_run_id,
            dedupe_key(r),
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


async def _retitle(conn: AsyncConnection, trip_id: str) -> None:
    """Refit a trip's destination and photo place, and its name unless its owner has renamed it, to
    everything now in it: what started as "Lisbon" becomes "Lisbon & Porto" once the Porto hotel is
    filed."""
    rs = await _reservations(conn, [trip_id])
    await conn.execute(
        """UPDATE trip SET destination = %s, place = %s,
               name = CASE WHEN name_edited THEN name ELSE %s END
           WHERE id = %s""",
        (grouping.pick_destination(rs), grouping.place_query(rs), grouping.trip_title(rs), trip_id),
    )


# --- users -----------------------------------------------------------------------------------
# A user is an app_user id. Email addresses (and, later, other identities) point at it; everything
# a user owns is keyed by the id, which is what `owner` means throughout.


def normalise_email(email: str) -> str:
    return email.strip().lower()


async def user_for_email(email: str) -> str:
    """The user an address belongs to. A new address gets a new user: invites can name an address
    nobody has yet (and the local SMTP gateway files for any sender). Inbound mail from the internet
    only ever looks addresses up (senders.owner), since that's what says who may file."""
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


# --- originals (inputs.py) --------------------------------------------------------------------


async def user_keysets(user_id: str) -> tuple[bytes, bytes] | None:
    """A user's encrypted (AEAD, PRF) keysets, if they have any yet."""
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT aead_keyset, prf_keyset FROM user_key WHERE user_id = %s", (user_id,)
        )
        row = await cur.fetchone()
    return (bytes(row[0]), bytes(row[1])) if row else None


async def add_user_keysets(user_id: str, aead_keyset: bytes, prf_keyset: bytes) -> tuple[bytes, bytes]:
    """Store a new user's keysets, unless someone else got there first: returns the ones that stuck."""
    async with connection() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO user_key (user_id, aead_keyset, prf_keyset) VALUES (%s, %s, %s)
               ON CONFLICT (user_id) DO NOTHING""",
            (user_id, aead_keyset, prf_keyset),
        )
        cur = await conn.execute(
            "SELECT aead_keyset, prf_keyset FROM user_key WHERE user_id = %s", (user_id,)
        )
        row = await cur.fetchone()
    assert row is not None
    return bytes(row[0]), bytes(row[1])


async def add_raw_input(i: RawInput, ingest_id: str) -> None:
    """Record a kept original (the same bytes again are already there) and link the ingest to it."""
    async with connection() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO raw_input (id, owner, sha256, object_key, size, source, content_type, filename,
                received_at)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (owner, sha256) DO NOTHING
            """,
            (i.id, i.owner, i.sha256, i.object_key, i.size, i.source, i.content_type, i.filename,
             i.received_at),
        )  # fmt: skip
        await conn.execute("UPDATE inbox_item SET input_id = %s WHERE ingest_id = %s", (i.id, ingest_id))


async def raw_input(input_id: str) -> RawInput | None:
    async with connection() as conn, conn.cursor(row_factory=class_row(RawInput)) as cur:
        await cur.execute(
            """SELECT id::text, owner::text, sha256, object_key, size, source, content_type, filename,
                      received_at
               FROM raw_input WHERE id = %s""",
            (input_id,),
        )
        return await cur.fetchone()


async def add_extraction_run(run: ExtractionRun) -> None:
    """Record a run, and make it the ingest's current one."""
    output = Jsonb(run.output.model_dump(mode="json")) if run.output is not None else None
    async with connection() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO extraction_run (id, ingest_id, owner, input_id, model, model_digest,
                prompt_version, code_version, params, purpose, output, error, examples)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (run.id, run.ingest_id, run.owner, run.input_id, run.model, run.model_digest,
             run.prompt_version, run.code_version, Jsonb(run.params), run.purpose, output, run.error,
             run.examples),
        )  # fmt: skip
        if run.purpose == "ingest":
            await conn.execute(
                "UPDATE inbox_item SET current_run_id = %s WHERE ingest_id = %s", (run.id, run.ingest_id)
            )


# --- the extraction eval set (evalset.py) -----------------------------------------------------


class EvalCase(BaseModel):
    """A shared ingest as the eval set copies it: its original and the run that made its reservations."""

    ingest_id: str
    label: str
    state: str
    input: RawInput
    run: ExtractionRun | None
    reservations: list[Reservation] = Field(default_factory=list)
    """What the owner has from it now, corrections and all."""
    keys: dict[str, str] = Field(default_factory=dict)
    """Their stored dedupe keys, by id (what `pair_up` matches the run's answer with)."""
    dismissals: list["Dismissal"] = Field(default_factory=list)
    """The ones the owner deleted."""


class EvalSharing(BaseModel):
    since: datetime
    copied: int
    """Ingests of theirs already in the eval bucket."""
    total: int
    """Ingests of theirs with a kept original."""


async def eval_sharing(user_id: str) -> EvalSharing | None:
    """Whether a user shares their originals with the eval set, and how far the copying has got."""
    async with connection() as conn:
        cur = await conn.execute(
            """SELECT s.created_at,
                      (SELECT count(*)::int FROM eval_copy c WHERE c.owner = s.user_id),
                      (SELECT count(*)::int FROM inbox_item i
                       WHERE i.owner = s.user_id AND i.input_id IS NOT NULL)
               FROM eval_share s WHERE s.user_id = %s""",
            (user_id,),
        )
        row = await cur.fetchone()
    return EvalSharing(since=row[0], copied=row[1], total=row[2]) if row else None


async def set_eval_sharing(user_id: str, share: bool) -> None:
    async with connection() as conn:
        if share:
            await conn.execute(
                "INSERT INTO eval_share (user_id) VALUES (%s) ON CONFLICT DO NOTHING", (user_id,)
            )
        else:
            await conn.execute("DELETE FROM eval_share WHERE user_id = %s", (user_id,))


async def eval_case(ingest_id: str) -> EvalCase | None:
    """None unless the ingest has a kept original and its owner shares with the eval set."""
    return await ingest_case(ingest_id, shared=True)


async def ingest_case(ingest_id: str, *, shared: bool = False) -> EvalCase | None:
    """An ingest with a kept original: the original, its current run and what its owner has from
    it now. None without a kept original (or, if `shared`, unless its owner shares with the eval
    set)."""
    async with connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            """
            SELECT i.ingest_id, i.label, i.state,
                   ri.id::text AS input_id, ri.owner::text, ri.sha256, ri.object_key, ri.size, ri.source,
                   ri.content_type, ri.filename, ri.received_at,
                   r.id::text AS run_id, r.model, r.model_digest, r.prompt_version, r.code_version,
                   r.params, r.purpose, r.output, r.error
            FROM inbox_item i
            JOIN raw_input ri ON ri.id = i.input_id
            LEFT JOIN extraction_run r ON r.id = i.current_run_id
            WHERE i.ingest_id = %(id)s
              AND (NOT %(shared)s OR EXISTS (SELECT 1 FROM eval_share s WHERE s.user_id = i.owner))
            """,
            {"id": ingest_id, "shared": shared},
        )
        row = await cur.fetchone()
        if row is None:
            return None
        await cur.execute(
            f"""SELECT {RESERVATION_COLUMNS}, dedupe_key FROM reservation
                WHERE trip_id IN (SELECT id FROM trip WHERE owner = %s) AND source_ingest_id = %s
                ORDER BY start_local, id""",
            (row["owner"], ingest_id),
        )
        stored = await cur.fetchall()
        dismissals = await _dismissals(conn, row["owner"], ingest_id)
    kept = RawInput(
        id=row["input_id"],
        owner=row["owner"],
        sha256=row["sha256"],
        object_key=row["object_key"],
        size=row["size"],
        source=row["source"],
        content_type=row["content_type"],
        filename=row["filename"],
        received_at=row["received_at"],
    )
    run = None
    if row["run_id"] is not None:
        run = ExtractionRun(
            id=row["run_id"],
            ingest_id=ingest_id,
            owner=kept.owner,
            input_id=kept.id,
            model=row["model"],
            model_digest=row["model_digest"],
            prompt_version=row["prompt_version"],
            code_version=row["code_version"],
            params=row["params"],
            purpose=row["purpose"],
            output=row["output"],
            error=row["error"],
        )
    return EvalCase(
        ingest_id=ingest_id,
        label=row["label"],
        state=row["state"],
        input=kept,
        run=run,
        reservations=[
            Reservation.model_validate({k: v for k, v in r.items() if k != "dedupe_key"}) for r in stored
        ],
        keys={r["id"]: r["dedupe_key"] for r in stored},
        dismissals=dismissals,
    )


# --- worked examples for extraction (examples.py) ---------------------------------------------


class Example(BaseModel):
    """An ingest whose reservations its owner corrected, as a worked example of reading it."""

    ingest_id: str
    owner: str
    sender_domain: str | None
    subject: str | None
    text: str
    """What the model is given (the parsed document's text)."""
    answer: Extraction
    """The run's answer as its owner corrected it."""
    changes: list[str]
    partial: bool
    """Some corrections couldn't be checked against `text`, so the model's answer was kept there."""
    embedding: list[float] | None = None
    """EMBED_MODEL's vector for the subject and text, when there is one."""


async def save_example(example: Example, settle: timedelta) -> None:
    """Write (or rewrite) an ingest's example, first usable `settle` from now."""
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO extraction_example (ingest_id, owner, sender_domain, subject, text, answer,
                   changes, partial, embedding, live_at)
               VALUES (%(ingest_id)s, %(owner)s, %(sender_domain)s, %(subject)s, %(text)s, %(answer)s,
                   %(changes)s, %(partial)s, %(embedding)s, now() + %(settle)s)
               ON CONFLICT (ingest_id) DO UPDATE SET
                   sender_domain = EXCLUDED.sender_domain, subject = EXCLUDED.subject,
                   text = EXCLUDED.text, answer = EXCLUDED.answer, changes = EXCLUDED.changes,
                   partial = EXCLUDED.partial, embedding = EXCLUDED.embedding, live_at = EXCLUDED.live_at,
                   updated_at = now()""",
            {
                **example.model_dump(exclude={"answer"}),
                "answer": Jsonb(example.answer.model_dump(mode="json")),
                "settle": settle,
            },
        )


async def delete_example(ingest_id: str) -> None:
    async with connection() as conn:
        await conn.execute("DELETE FROM extraction_example WHERE ingest_id = %s", (ingest_id,))


class LiveExample(Example):
    received_at: datetime


async def live_examples(owner: str) -> list[LiveExample]:
    """A user's examples past their settle time, newest first."""
    async with connection() as conn, conn.cursor(row_factory=class_row(LiveExample)) as cur:
        await cur.execute(
            """SELECT e.ingest_id, e.owner::text, sender_domain, subject, text, answer, changes, partial,
                      embedding, i.received_at
               FROM extraction_example e JOIN inbox_item i USING (ingest_id)
               WHERE e.owner = %s AND live_at <= now()
               ORDER BY updated_at DESC, e.ingest_id""",
            (owner,),
        )
        return await cur.fetchall()


async def mark_eval_copied(ingest_id: str, owner: str) -> None:
    async with connection() as conn:
        await conn.execute(
            "INSERT INTO eval_copy (ingest_id, owner) VALUES (%s, %s) ON CONFLICT (ingest_id) DO NOTHING",
            (ingest_id, owner),
        )


async def uncopied_eval_ingests(owner: str) -> list[str]:
    """A sharing user's ingests with a kept original that aren't in the eval bucket yet, oldest first."""
    async with connection() as conn:
        cur = await conn.execute(
            """SELECT i.ingest_id FROM inbox_item i
               JOIN eval_share s ON s.user_id = i.owner
               WHERE i.owner = %s AND i.input_id IS NOT NULL
                 AND NOT EXISTS (SELECT 1 FROM eval_copy c WHERE c.ingest_id = i.ingest_id)
               ORDER BY i.received_at, i.ingest_id""",
            (owner,),
        )
        return [r[0] for r in await cur.fetchall()]


# --- writes (called from Restate handlers inside ctx.run) -------------------------------------


class FiledReservation(BaseModel):
    trip_id: str
    reservation: Reservation


class Filed(BaseModel):
    trip_ids: list[str]
    reservations: list[FiledReservation]
    """Every reservation in the touched trips, as stored. Not just the new ones: filing can fill in
    the timezone of an earlier segment (hotel first, flight later), making its reminder possible."""


class Dismissal(BaseModel):
    """A reservation the user deleted, as the ingest it came from described it."""

    dedupe_key: str
    kind: str
    confirmation_code: str | None
    start_date: date
    city: str | None


def dismissed(r: Reservation, d: Dismissal) -> bool:
    """Whether `r`, from the same ingest as `d`, is the reservation the user deleted. A re-extraction
    can word a segment differently, so after the exact key, the same booking code on the same day,
    then the same kind of thing on the same day in the same city, count as the same."""
    if dedupe_key(r) == d.dedupe_key:
        return True
    if r.kind.value != d.kind or r.start_date != d.start_date:
        return False
    if r.confirmation_code and r.confirmation_code == d.confirmation_code:
        return True
    return bool(r.city) and r.city == d.city


# Records what's about to be deleted (the reservations matching the WHERE clause that follows).
DISMISS = """
    INSERT INTO dismissed_reservation (owner, ingest_id, dedupe_key, kind, confirmation_code,
        start_date, city)
    SELECT t.owner, r.source_ingest_id, r.dedupe_key, r.kind, r.confirmation_code, r.start_local::date,
           r.city
    FROM reservation r JOIN trip t ON t.id = r.trip_id
    WHERE r.source_ingest_id IS NOT NULL AND t.owner = %s
"""


async def _file(
    conn: AsyncConnection, owner: str, trip_key: str, reservations: list[Reservation]
) -> list[str]:
    """Put new reservations into trips (existing ones, or new ones named by `trip_key`)."""
    counter = iter(range(len(reservations) + 1))
    buckets = grouping.assign(
        reservations, await _summaries(conn, owner), lambda: new_trip_id(trip_key, next(counter))
    )
    for b in buckets:
        for absorbed in b.absorbed:
            await _merge_trip(conn, absorbed, b.trip_id)
        if b.is_new:
            await conn.execute(
                "INSERT INTO trip (id, owner, name, destination) VALUES (%s, %s, %s, %s)",
                (b.trip_id, owner, grouping.trip_title(b.reservations), b.destination),
            )
        for r in b.reservations:
            await _upsert_reservation(conn, b.trip_id, r)
        await _fill_timezones(conn, b.trip_id)
        await _retitle(conn, b.trip_id)
    return [b.trip_id for b in buckets]


async def _dismissals(conn: AsyncConnection, owner: str, ingest_id: str) -> list[Dismissal]:
    async with conn.cursor(row_factory=class_row(Dismissal)) as cur:
        await cur.execute(
            """SELECT dedupe_key, kind, confirmation_code, start_date, city
               FROM dismissed_reservation WHERE owner = %s AND ingest_id = %s""",
            (owner, ingest_id),
        )
        return await cur.fetchall()


async def _stored(conn: AsyncConnection, trip_ids: list[str]) -> Filed:
    return Filed(
        trip_ids=trip_ids,
        reservations=[
            FiledReservation(trip_id=tid, reservation=r)
            for tid in trip_ids
            for r in await _reservations(conn, [tid])
        ],
    )


async def file_reservations(owner: str, ingest_id: str, reservations: list[Reservation]) -> Filed:
    async with connection() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM trip WHERE owner = %s FOR UPDATE", (owner,))
        gone = await _dismissals(conn, owner, ingest_id)
        reservations = [r for r in reservations if not any(dismissed(r, d) for d in gone)]
        ids = [r.id for r in reservations]

        cur = await conn.execute(
            "SELECT DISTINCT trip_id::text FROM reservation WHERE id = ANY(%s::uuid[])", (ids,)
        )
        already = [row[0] for row in await cur.fetchall()]
        if not already and reservations:
            already = await _file(conn, owner, ingest_id, reservations)
            await _changed(conn, owner)
        return await _stored(conn, already)


# --- reprocessing (services/reprocess.py) ----------------------------------------------------
# A new extraction of an ingest proposes; what the user did wins. Its reservations are matched to
# the ones the ingest already produced, matches are updated in place (keeping their ids, which key
# their reminders), new ones are filed, and nothing is ever deleted: a reservation the new run
# doesn't mention is only reported. Deleted ones (dismissed_reservation) stay deleted.


def _shown(value: object) -> str | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat(sep=" ", timespec="minutes")
    if isinstance(value, list):
        return ", ".join(map(str, value))
    return str(value)


class FieldChange(BaseModel):
    field: str
    old: str | None
    new: str | None


class Change(BaseModel):
    id: str
    title: str
    fields: list[FieldChange]


class Skipped(BaseModel):
    reservation: Reservation
    reason: Literal["dismissed", "duplicate"]
    """dismissed: the user deleted it. duplicate: the same segment is already filed (from another
    ingest, or twice in this run)."""


class Reconciliation(BaseModel):
    """What a run would do to an ingest's reservations."""

    updated: list[Reservation] = Field(default_factory=list)
    """Every matched reservation as the run has it, under the id of the one it matches."""
    changes: list[Change] = Field(default_factory=list)
    unchanged: int = 0
    added: list[Reservation] = Field(default_factory=list)
    skipped: list[Skipped] = Field(default_factory=list)
    missing: list[Reservation] = Field(default_factory=list)
    """The ingest's reservations the run didn't produce. Kept."""


def _as_dismissal(r: Reservation) -> Dismissal:
    return Dismissal(
        dedupe_key=dedupe_key(r), kind=r.kind.value, confirmation_code=r.confirmation_code,
        start_date=r.start_date, city=r.city,
    )  # fmt: skip


def reclassified(c: Reservation, e: Reservation) -> bool:
    """Whether `c` is `e` called something else: the same departure to the minute, with no booking
    code that says otherwise. A newer run can know a kind the old one didn't (a ferry that was
    filed as `other`), and should change it rather than file the segment twice."""
    if c.kind == e.kind or c.start_local != e.start_local:
        return False
    return not (c.confirmation_code and e.confirmation_code) or c.confirmation_code == e.confirmation_code


def pair_up(
    current: list[Reservation], candidates: list[Reservation], keys: dict[str, str]
) -> dict[int, Reservation]:
    """Which of the `current` reservations each of a run's `candidates` (by index) is, one to one:
    the exact key first, then the looser rules `dismissed` uses, then the same departure under
    another kind (`reclassified`). `keys` are the current ones' stored dedupe keys, which are what
    their extraction said even where the owner has corrected them since."""
    free = list(current)
    match: dict[int, Reservation] = {}

    def pair(same: Callable[[Reservation, Reservation], bool]) -> None:
        for i, c in enumerate(candidates):
            if i in match:
                continue
            if (e := next((e for e in free if same(c, e)), None)) is not None:
                match[i] = e
                free.remove(e)

    pair(lambda c, e: dedupe_key(c) in (keys.get(e.id), dedupe_key(e)))
    pair(lambda c, e: dismissed(c, _as_dismissal(e)))
    pair(reclassified)
    return match


def reconcile(
    current: list[Reservation],
    candidates: list[Reservation],
    gone: list[Dismissal],
    taken: set[str],
    keys: dict[str, str] | None = None,
) -> Reconciliation:
    """Match a run's reservations (`candidates`) to the ingest's `current` ones (`pair_up`, which
    `keys` is for). `taken` is the dedupe key of every reservation the user has. A match keeps every
    field its owner corrected."""
    match = pair_up(current, candidates, keys or {})
    paired = {e.id for e in match.values()}
    out = Reconciliation(missing=[e for e in current if e.id not in paired])
    taken = taken | {dedupe_key(candidates[i]) for i in match}
    for i, c in enumerate(candidates):
        if (e := match.get(i)) is not None:
            # Timezones are often inferred after extraction (from the trip's other segments), so a
            # run that doesn't name one leaves the known one.
            u = c.model_copy(
                update={
                    "id": e.id,
                    "start_tz": c.start_tz or e.start_tz,
                    "end_tz": c.end_tz or e.end_tz,
                    **{f: getattr(e, f) for f in e.edited_fields},
                    "edited_fields": e.edited_fields,
                }
            )
            out.updated.append(u)
            fields = [
                FieldChange(field=f, old=_shown(getattr(e, f)), new=_shown(getattr(u, f)))
                for f in EDITABLE  # all but what the owner corrected, which `u` keeps
                if getattr(e, f) != getattr(u, f)
            ]
            if fields:
                out.changes.append(Change(id=e.id, title=e.title, fields=fields))
            else:
                out.unchanged += 1
        elif any(dismissed(c, d) for d in gone):
            out.skipped.append(Skipped(reservation=c, reason="dismissed"))
        elif dedupe_key(c) in taken:
            out.skipped.append(Skipped(reservation=c, reason="duplicate"))
        else:
            out.added.append(c)
            taken.add(dedupe_key(c))
    return out


async def _reconciliation(
    conn: AsyncConnection, owner: str, ingest_id: str, candidates: list[Reservation]
) -> Reconciliation:
    async with conn.cursor(row_factory=class_row(Reservation)) as cur:
        await cur.execute(
            f"""SELECT {RESERVATION_COLUMNS} FROM reservation
                WHERE trip_id IN (SELECT id FROM trip WHERE owner = %s) AND source_ingest_id = %s
                ORDER BY start_local""",
            (owner, ingest_id),
        )
        current = await cur.fetchall()
    cur = await conn.execute(
        """SELECT id::text, dedupe_key, source_ingest_id FROM reservation
           WHERE trip_id IN (SELECT id FROM trip WHERE owner = %s)""",
        (owner,),
    )
    rows = await cur.fetchall()
    keys = {rid: key for rid, key, ingest in rows if ingest == ingest_id}
    taken = {key for _, key, _ in rows}
    return reconcile(current, candidates, await _dismissals(conn, owner, ingest_id), taken, keys)


async def preview_reprocess(owner: str, ingest_id: str, candidates: list[Reservation]) -> Reconciliation:
    async with connection() as conn:
        return await _reconciliation(conn, owner, ingest_id, candidates)


class Reprocessed(BaseModel):
    reconciliation: Reconciliation
    filed: Filed
    """The touched trips, for reminders and photos."""


async def apply_reprocess(
    owner: str, ingest_id: str, run_id: str, candidates: list[Reservation]
) -> Reprocessed:
    """Make `run_id` the ingest's current run. Safe to re-run: a second pass matches everything the
    first one filed."""
    async with connection() as conn, conn.transaction():
        await conn.execute("SELECT 1 FROM trip WHERE owner = %s FOR UPDATE", (owner,))
        rec = await _reconciliation(conn, owner, ingest_id, candidates)
        touched: list[str] = []
        for u in rec.updated:
            # Skipped if the new key would clash with another segment of the same trip.
            cur = await conn.execute(
                """
                UPDATE reservation a SET kind = %s, title = %s, provider = %s, confirmation_code = %s,
                    start_local = %s, start_tz = %s, end_local = %s, end_tz = %s,
                    start_location = %s, end_location = %s, address = %s, city = %s,
                    service_number = %s, seat = %s, travelers = %s, notes = %s, source_run_id = %s,
                    -- A corrected one keeps the key its owner's edits were made against.
                    dedupe_key = CASE WHEN a.edited_fields = '{}' THEN %s ELSE a.dedupe_key END
                WHERE a.id = %s AND (a.edited_fields <> '{}' OR NOT EXISTS (SELECT 1 FROM reservation b
                    WHERE b.trip_id = a.trip_id AND b.dedupe_key = %s AND b.id <> a.id))
                RETURNING trip_id::text
                """,
                (u.kind.value, u.title, u.provider, u.confirmation_code, u.start_local, u.start_tz,
                 u.end_local, u.end_tz, u.start_location, u.end_location, u.address, u.city, u.service_number,
                 u.seat, u.travelers, u.notes, run_id, dedupe_key(u), u.id, dedupe_key(u)),
            )  # fmt: skip
            if (row := await cur.fetchone()) is not None and row[0] not in touched:
                touched.append(row[0])
        for tid in touched:
            await _fill_timezones(conn, tid)
            await _retitle(conn, tid)
        if rec.added:
            for tid in await _file(conn, owner, f"{ingest_id}@{run_id}", rec.added):
                if tid not in touched:
                    touched.append(tid)
        await conn.execute(
            """
            WITH mine AS (
                SELECT DISTINCT r.trip_id FROM reservation r JOIN trip t ON t.id = r.trip_id
                WHERE t.owner = %(owner)s AND r.source_ingest_id = %(ingest)s
            )
            UPDATE inbox_item SET current_run_id = %(run)s,
                trip_ids = ARRAY(SELECT trip_id FROM mine),
                message = CASE WHEN state <> 'done' AND EXISTS (SELECT 1 FROM mine)
                               THEN 'Filed when reprocessed.' ELSE message END,
                state = CASE WHEN EXISTS (SELECT 1 FROM mine) THEN 'done' ELSE state END
            WHERE ingest_id = %(ingest)s AND owner = %(owner)s
            """,
            {"owner": owner, "ingest": ingest_id, "run": run_id},
        )
        await _changed(conn, owner)
        return Reprocessed(reconciliation=rec, filed=await _stored(conn, touched))


class ReprocessTarget(BaseModel):
    owner: str
    input_id: str | None
    received_at: datetime


async def reprocess_target(ingest_id: str) -> ReprocessTarget:
    async with connection() as conn, conn.cursor(row_factory=class_row(ReprocessTarget)) as cur:
        await cur.execute(
            "SELECT owner::text, input_id::text, received_at FROM inbox_item WHERE ingest_id = %s",
            (ingest_id,),
        )
        target = await cur.fetchone()
    if target is None:
        raise restate.TerminalError(f"no ingest {ingest_id}", status_code=404)
    return target


async def run_output(run_id: str) -> Extraction:
    async with connection() as conn:
        cur = await conn.execute("SELECT output FROM extraction_run WHERE id = %s", (run_id,))
        row = await cur.fetchone()
    if row is None or row[0] is None:
        raise restate.TerminalError(f"run {run_id} has no output to re-derive from", status_code=404)
    return Extraction.model_validate(row[0])


async def add_reprocess(req: ReprocessRequest, owner: str) -> None:
    async with connection() as conn:
        await conn.execute(
            """INSERT INTO reprocess (id, batch_id, ingest_id, owner, mode, model, from_run_id)
               VALUES (%s, %s, %s, %s, %s, %s, %s) ON CONFLICT (id) DO NOTHING""",
            (req.key, req.batch_id, req.ingest_id, owner, req.mode, req.model, req.from_run_id),
        )


async def finish_reprocess(
    key: str, run_id: str | None, rec: Reconciliation | None, error: str | None
) -> None:
    diff = Jsonb(rec.model_dump(mode="json", exclude={"updated"})) if rec is not None else None
    async with connection() as conn:
        await conn.execute(
            """UPDATE reprocess SET run_id = %s, diff = %s, error = %s, finished_at = now(),
                   state = CASE WHEN %s::text IS NULL THEN 'done' ELSE 'failed' END
               WHERE id = %s""",
            (run_id, diff, error, error, key),
        )


async def _owned(conn: AsyncConnection, owner: str, trip_id: str) -> None:
    cur = await conn.execute("SELECT 1 FROM trip WHERE id = %s AND owner = %s FOR UPDATE", (trip_id, owner))
    if await cur.fetchone() is None:
        raise restate.TerminalError("trip not found", status_code=404)


class Removed(BaseModel):
    trip_exists: bool
    """False when the trip went with its last reservation."""
    ingest_id: str | None
    """Where the reservation came from (None for one already gone)."""


async def remove_reservation(owner: str, trip_id: str, reservation_id: str) -> Removed:
    async with connection() as conn, conn.transaction():
        await _owned(conn, owner, trip_id)
        await conn.execute(
            DISMISS + " AND r.id = %s AND r.trip_id = %s ON CONFLICT DO NOTHING",
            (owner, reservation_id, trip_id),
        )
        cur = await conn.execute(
            "DELETE FROM reservation WHERE id = %s AND trip_id = %s RETURNING source_ingest_id",
            (reservation_id, trip_id),
        )
        gone = await cur.fetchone()
        ingest_id = gone[0] if gone else None
        await _changed(conn, owner)
        cur = await conn.execute("SELECT 1 FROM reservation WHERE trip_id = %s LIMIT 1", (trip_id,))
        if await cur.fetchone() is None:
            await conn.execute("DELETE FROM trip WHERE id = %s", (trip_id,))
            return Removed(trip_exists=False, ingest_id=ingest_id)
        await _retitle(conn, trip_id)
        return Removed(trip_exists=True, ingest_id=ingest_id)


async def rename_trip(owner: str, trip_id: str, name: str) -> None:
    async with connection() as conn, conn.transaction():
        await _owned(conn, owner, trip_id)
        if name.strip():
            await conn.execute(
                "UPDATE trip SET name = %s, name_edited = true WHERE id = %s", (name.strip(), trip_id)
            )
            await _changed(conn, owner)


async def merge_trips(owner: str, trip_id: str, into: str) -> None:
    """Move everything in `trip_id` into `into` and delete it: the owner dropped one trip on another.
    `into` keeps its id and its name if its owner renamed it, else takes the other's name if that
    was renamed, else is retitled for everything now in it.

    Nothing needs remembering for the merge to last. Filing never moves a filed reservation, and
    reprocessing updates them in place, so they stay in `into`; and new bookings in or near either
    trip's dates overlap the merged one, so they're filed into it too.

    Safe to re-run: once `trip_id` is gone there's nothing to merge."""
    if trip_id == into:
        raise restate.TerminalError("can't merge a trip into itself", status_code=400)
    async with connection() as conn, conn.transaction():
        await _owned(conn, owner, into)
        cur = await conn.execute(
            "SELECT 1 FROM trip WHERE id = %s AND owner = %s FOR UPDATE", (trip_id, owner)
        )
        if await cur.fetchone() is None:
            return
        await conn.execute(
            """UPDATE trip t SET name = s.name, name_edited = true FROM trip s
               WHERE t.id = %s AND s.id = %s AND s.name_edited AND NOT t.name_edited""",
            (into, trip_id),
        )
        await _merge_trip(conn, trip_id, into)
        await _fill_timezones(conn, into)
        await _retitle(conn, into)
        await _changed(conn, owner)


async def edit_reservation(
    owner: str, trip_id: str, reservation_id: str, fields: ReservationFields
) -> Filed | None:
    """Correct a reservation. Every field that changes is marked as the owner's, so filing and
    reprocessing leave it be. Returns the trip as stored (a new time means new reminders), or None
    if nothing changed."""
    async with connection() as conn, conn.transaction():
        await _owned(conn, owner, trip_id)
        async with conn.cursor(row_factory=class_row(Reservation)) as cur:
            await cur.execute(
                f"SELECT {RESERVATION_COLUMNS} FROM reservation WHERE id = %s AND trip_id = %s",
                (reservation_id, trip_id),
            )
            r = await cur.fetchone()
        if r is None:
            raise restate.TerminalError("reservation not found", status_code=404)
        changed: list[LiteralString] = [f for f in EDITABLE if getattr(fields, f) != getattr(r, f)]
        if not changed:
            return None
        values = fields.model_dump(include=set(changed))
        if "kind" in values:
            values["kind"] = fields.kind.value
        await conn.execute(
            f"""UPDATE reservation SET {", ".join(f"{f} = %({f})s" for f in changed)},
                    edited_fields = %(edited)s
                WHERE id = %(id)s""",
            {**values, "edited": sorted({*r.edited_fields, *changed}), "id": reservation_id},
        )
        await _fill_timezones(conn, trip_id)
        await _retitle(conn, trip_id)
        await _changed(conn, owner)
        return await _stored(conn, [trip_id])


async def delete_trip(owner: str, trip_id: str) -> None:
    async with connection() as conn, conn.transaction():
        await conn.execute(DISMISS + " AND r.trip_id = %s ON CONFLICT DO NOTHING", (owner, trip_id))
        await conn.execute("DELETE FROM trip WHERE id = %s AND owner = %s", (trip_id, owner))
        await _changed(conn, owner)


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
        await _changed(conn, owner)


async def add_inbox_item(owner: str, item: InboxItem) -> None:
    """The row for an ingest that was only just started, unless the workflow got there first."""
    async with connection() as conn, conn.transaction():
        cur = await conn.execute(
            """
            INSERT INTO inbox_item (ingest_id, owner, source, label, received_at, state, message, trip_ids)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s::uuid[])
            ON CONFLICT (ingest_id) DO NOTHING
            """,
            (item.ingest_id, owner, item.source, item.label, item.received_at, item.state, item.message,
             item.trip_ids),
        )  # fmt: skip
        if cur.rowcount:
            await _changed(conn, owner)


async def add_notification(owner: str, n: Notification) -> None:
    async with connection() as conn, conn.transaction():
        await conn.execute(
            """INSERT INTO notification (id, owner, at, trip_id, text) VALUES (%s, %s, %s, %s, %s)
               ON CONFLICT (id) DO NOTHING""",
            (n.id, owner, n.at, n.trip_id, n.text),
        )
        await _changed(conn, owner)


async def dismiss_notifications(owner: str) -> None:
    async with connection() as conn, conn.transaction():
        await conn.execute("DELETE FROM notification WHERE owner = %s", (owner,))
        await _changed(conn, owner)


async def places_needing_photos(trip_ids: list[str] | None = None) -> list[str]:
    """The places of these trips (all trips if None) that haven't been looked up yet, or whose
    photo's subject hasn't been found yet."""
    async with connection() as conn:
        cur = await conn.execute(
            """SELECT DISTINCT t.place FROM trip t LEFT JOIN place_photo p ON p.place = t.place
               WHERE t.place IS NOT NULL AND (%(ids)s::uuid[] IS NULL OR t.id = ANY(%(ids)s::uuid[]))
                 AND (p.place IS NULL OR (p.image IS NOT NULL AND NOT p.subject_checked))""",
            {"ids": trip_ids},
        )
        return [row[0] for row in await cur.fetchall()]


class PhotoState(BaseModel):
    looked_up: bool = False
    """Whether there's a row: a photo, or a note that there's none."""
    needs_subject: bool = False


async def place_photo_state(place: str) -> PhotoState:
    async with connection() as conn:
        cur = await conn.execute(
            "SELECT image IS NOT NULL AND NOT subject_checked FROM place_photo WHERE place = %s", (place,)
        )
        row = await cur.fetchone()
    return PhotoState(looked_up=True, needs_subject=row[0]) if row else PhotoState()


async def place_photo_image(place: str) -> bytes | None:
    async with connection() as conn:
        cur = await conn.execute("SELECT image FROM place_photo WHERE place = %s", (place,))
        row = await cur.fetchone()
    return bytes(row[0]) if row and row[0] is not None else None


async def _photo_changed(conn: AsyncConnection, place: str) -> None:
    cur = await conn.execute("SELECT DISTINCT owner::text FROM trip WHERE place = %s", (place,))
    for (owner,) in await cur.fetchall():
        await _changed(conn, owner)


async def save_place_photo(
    place: str,
    choice: PhotoChoice | None,
    image: bytes | None,
    content_type: str | None,
    aspect: float | None = None,
) -> None:
    """Record a place's photo, or with no image that there's none to be had. Its subject is found
    afterwards (save_photo_subject)."""
    if image is None:
        choice = None
    async with connection() as conn, conn.transaction():
        await conn.execute(
            """
            INSERT INTO place_photo (place, image, content_type, file, page_url, article_url, artist,
                licence, licence_url, aspect)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (place) DO UPDATE SET image = EXCLUDED.image,
                content_type = EXCLUDED.content_type, file = EXCLUDED.file,
                page_url = EXCLUDED.page_url, article_url = EXCLUDED.article_url,
                artist = EXCLUDED.artist, licence = EXCLUDED.licence,
                licence_url = EXCLUDED.licence_url, aspect = EXCLUDED.aspect, subject = NULL,
                subject_checked = false, fetched_at = now()
            """,
            (
                place, image, content_type if image else None, choice and choice.file,
                choice and choice.page_url, choice and choice.article_url, choice and choice.artist,
                choice and choice.licence, choice and choice.licence_url, aspect if image else None,
            ),
        )  # fmt: skip
        await _photo_changed(conn, place)


async def save_photo_subject(place: str, box: list[float] | None, aspect: float | None = None) -> None:
    """Where the photo's main subject is (None: the model found no usable one, so the default crop
    stays). `aspect` fills it in for photos stored before it was recorded."""
    async with connection() as conn, conn.transaction():
        await conn.execute(
            """UPDATE place_photo SET subject = %s, subject_checked = true,
                   aspect = coalesce(aspect, %s)
               WHERE place = %s""",
            (box, aspect, place),
        )
        await _photo_changed(conn, place)


async def fill_places() -> None:
    """Give trips from before photos their place (what `_retitle` would set). Not under the User
    object: it only fills a column that's empty, and filing sets it to the same value."""
    async with connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT id::text FROM trip WHERE place IS NULL")
        for (trip_id,) in await cur.fetchall():
            place = grouping.place_query(await _reservations(conn, [trip_id]))
            if place is not None:
                await conn.execute(
                    "UPDATE trip SET place = %s WHERE id = %s AND place IS NULL", (place, trip_id)
                )


async def refill_timezones() -> None:
    """Re-run `_fill_timezones` on every trip, for reservations filed before it knew better (a gap,
    or the model's guess where the city says otherwise). Not under the User object: it only does
    what filing would, and writes only the rows it changes."""
    async with connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT id::text FROM trip")
        for (trip_id,) in await cur.fetchall():
            await _fill_timezones(conn, trip_id)


async def retitle_trips() -> None:
    """Re-run `_retitle` on every trip, for names and places picked before `grouping` knew better (a
    flights-only trip named after its first stop). Not under the User object: it only does what filing
    would, and leaves names their owners chose."""
    async with connection() as conn, conn.transaction():
        cur = await conn.execute("SELECT id::text FROM trip")
        for (trip_id,) in await cur.fetchall():
            await _retitle(conn, trip_id)


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
    async with connection() as conn, conn.cursor(row_factory=dict_row) as cur:
        await cur.execute(
            f"""SELECT t.name, t.destination, {PHOTO_COLUMNS} FROM trip t {PHOTO_JOIN}
                WHERE t.id = %s AND t.owner = %s""",
            (trip_id, owner),
        )
        row = await cur.fetchone()
        if row is None:
            return None
        reservations = await _reservations(conn, [trip_id])
    return Trip(id=trip_id, owner=owner, photo=_photo(row), reservations=reservations, **row)


async def trip_photo(owner: str, trip_id: str) -> tuple[bytes, str] | None:
    """The image for a trip's photo and its content type."""
    async with connection() as conn:
        cur = await conn.execute(
            """SELECT p.image, p.content_type FROM trip t JOIN place_photo p ON p.place = t.place
               WHERE t.id = %s AND t.owner = %s AND p.image IS NOT NULL""",
            (trip_id, owner),
        )
        row = await cur.fetchone()
    return (bytes(row[0]), row[1]) if row else None


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
    has_original: bool = False
    """Its original was kept, so it can be extracted again."""
    has_run: bool = False
    """It has a recorded run, so it can be re-derived without the GPU."""


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
                                ORDER BY created_at, email LIMIT 1), owner::text) AS owner,
                      input_id IS NOT NULL AS has_original, current_run_id IS NOT NULL AS has_run
               FROM inbox_item i WHERE %(state)s::text IS NULL OR state = %(state)s
               ORDER BY received_at DESC LIMIT %(limit)s""",
            {"state": state, "limit": limit},
        )
        return await cur.fetchall()


class ReprocessCandidate(BaseModel):
    ingest_id: str
    owner: str
    input_id: str | None
    current_run_id: str | None


async def reprocess_candidates(
    *,
    ingest_id: str | None = None,
    state: str | None = None,
    email: str | None = None,
    since: date | None = None,
    limit: int = 50,
) -> list[ReprocessCandidate]:
    """Finished ingests matching the admin's filter, newest first."""
    async with connection() as conn, conn.cursor(row_factory=class_row(ReprocessCandidate)) as cur:
        await cur.execute(
            """SELECT ingest_id, owner::text, input_id::text, current_run_id::text FROM inbox_item
               WHERE state IN ('done', 'no_travel', 'failed')
                 AND (%(ingest)s::text IS NULL OR ingest_id = %(ingest)s)
                 AND (%(state)s::text IS NULL OR state = %(state)s)
                 AND (%(email)s::text IS NULL
                      OR owner IN (SELECT user_id FROM user_email WHERE email = %(email)s))
                 AND (%(since)s::date IS NULL OR received_at >= %(since)s)
               ORDER BY received_at DESC LIMIT %(limit)s""",
            {
                "ingest": ingest_id,
                "state": state,
                "email": email and normalise_email(email),
                "since": since,
                "limit": limit,
            },
        )
        return await cur.fetchall()


class ReprocessBatch(BaseModel):
    batch_id: str
    mode: str
    model: str | None
    started: datetime
    total: int
    queued: int
    failed: int
    changed: int
    added: int
    missing: int
    skipped: int


async def reprocess_batches(limit: int = 20) -> list[ReprocessBatch]:
    async with connection() as conn, conn.cursor(row_factory=class_row(ReprocessBatch)) as cur:
        await cur.execute(
            """SELECT batch_id, min(mode) AS mode, min(model) AS model, min(created_at) AS started,
                      count(*)::int AS total,
                      count(*) FILTER (WHERE state = 'queued')::int AS queued,
                      count(*) FILTER (WHERE state = 'failed')::int AS failed,
                      coalesce(sum(jsonb_array_length(diff -> 'changes')), 0)::int AS changed,
                      coalesce(sum(jsonb_array_length(diff -> 'added')), 0)::int AS added,
                      coalesce(sum(jsonb_array_length(diff -> 'missing')), 0)::int AS missing,
                      coalesce(sum(jsonb_array_length(diff -> 'skipped')), 0)::int AS skipped
               FROM reprocess GROUP BY batch_id ORDER BY max(created_at) DESC LIMIT %s""",
            (limit,),
        )
        return await cur.fetchall()


class ReprocessItem(BaseModel):
    id: str
    ingest_id: str
    owner: str
    """The owner's primary address (their user id if they have none)."""
    label: str | None
    mode: str
    state: str
    model: str | None
    """The model whose output was used."""
    run_id: str | None
    diff: Reconciliation | None
    error: str | None


async def reprocess_batch(batch_id: str, limit: int = 200) -> list[ReprocessItem]:
    async with connection() as conn, conn.cursor(row_factory=class_row(ReprocessItem)) as cur:
        await cur.execute(
            """SELECT p.id, p.ingest_id, i.label, p.mode, p.state,
                      coalesce((SELECT email FROM user_email e WHERE e.user_id = p.owner
                                ORDER BY created_at, email LIMIT 1), p.owner::text) AS owner,
                      coalesce(r.model, p.model) AS model, p.run_id::text, p.diff, p.error
               FROM reprocess p
               LEFT JOIN inbox_item i ON i.ingest_id = p.ingest_id
               LEFT JOIN extraction_run r ON r.id = p.run_id
               WHERE p.batch_id = %s ORDER BY p.created_at DESC LIMIT %s""",
            (batch_id, limit),
        )
        return await cur.fetchall()


async def reprocess_runs(batch_id: str) -> list[tuple[str, str, str]]:
    """(ingest id, owner, run id) of each finished dry or shadow run in a batch, to apply them."""
    async with connection() as conn:
        cur = await conn.execute(
            """SELECT ingest_id, owner::text, run_id::text FROM reprocess
               WHERE batch_id = %s AND state = 'done' AND run_id IS NOT NULL AND mode <> 'apply'""",
            (batch_id,),
        )
        return [(i, o, r) for i, o, r in await cur.fetchall()]
