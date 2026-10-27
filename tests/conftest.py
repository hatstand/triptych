"""Shared fixtures. Database tests run in a throwaway database built from db/schema.sql, and are
skipped unless TEST_DATABASE_URL points at a server we may CREATE DATABASE on (Tilt sets it to the
compose Postgres)."""

import os
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import LiteralString, cast

import psycopg
import pytest
from psycopg import conninfo, sql

from triptych import db, sharing
from triptych.models import Role, TripAccess

ADMIN_URL = os.environ.get("TEST_DATABASE_URL")
SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def database() -> AsyncIterator[str]:
    """The URL of a fresh database, which `db` is pointed at."""
    assert ADMIN_URL
    name = f"triptych_test_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
        admin.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
    url = conninfo.make_conninfo(ADMIN_URL, dbname=name)
    # pg_dump ≥ 17.6 wraps dumps in psql-only \restrict lines; strip meta-commands.
    ddl = "\n".join(line for line in SCHEMA.read_text().splitlines() if not line.startswith("\\"))
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(cast(LiteralString, ddl))  # our own generated schema dump, not user input
    db.init(url)
    try:
        yield url
    finally:
        await db.close()
        with psycopg.connect(ADMIN_URL, autocommit=True) as admin:
            admin.execute(sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(name)))


@pytest.fixture
def own_every_trip(monkeypatch: pytest.MonkeyPatch) -> None:
    """Web tests without a database: whoever asks owns every trip, except ones named someone-elses*,
    and none of them is shared."""

    async def trip_access(user_id: str, trip_id: str) -> TripAccess | None:
        if trip_id.startswith("someone-elses"):
            return None
        return TripAccess(trip_id=trip_id, owner=user_id, role=Role.OWNER)

    async def nothing(*_: object) -> list[object]:
        return []

    monkeypatch.setattr(db, "trip_access", trip_access)
    monkeypatch.setattr(db, "shared_trip_summaries", nothing)
    for unshared in ("links", "members", "pending_invites"):
        monkeypatch.setattr(sharing, unshared, nothing)
