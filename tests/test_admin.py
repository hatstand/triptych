from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from triptych import db
from triptych.admin import app as admin

OWNER = "john.maguire@gmail.com"
FAILED = db.AdminInboxItem(
    ingest_id="web-1", owner="you@example.com", source="upload", label="boarding pass.pdf",
    received_at=datetime(2026, 10, 23, 9, 30, tzinfo=UTC), state="failed", message="LLM timed out",
)  # fmt: skip


@pytest.fixture
def filters(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str | None]]:
    """Stub out Postgres and Ollama; yields the state filters the page asked the db for."""
    seen: list[str | None] = []

    async def ingest_counts(since: datetime) -> dict[str, int]:
        return {"failed": 2, "done": 5}

    async def recent_ingests(state: str | None = None, limit: int = 100) -> list[db.AdminInboxItem]:
        seen.append(state)
        return [FAILED]

    async def health() -> dict[str, Any]:
        raise ConnectionError("ollama unreachable")

    monkeypatch.setattr(admin, "settings", replace(admin.settings, admin_logins=frozenset({OWNER})))
    monkeypatch.setattr(admin.db, "ingest_counts", ingest_counts)
    monkeypatch.setattr(admin.db, "recent_ingests", recent_ingests)
    monkeypatch.setattr(admin.llm, "health", health)
    yield seen


# Not entered as a context manager, so the lifespan never opens a Postgres pool.
client = TestClient(admin.app)


def as_login(login: str) -> dict[str, str]:
    return {"Tailscale-User-Login": login}


def test_no_tailscale_identity_is_refused(filters: list[str | None]) -> None:
    r = client.get("/")
    assert r.status_code == 403
    assert "no Tailscale identity" in r.text


def test_login_outside_the_allowlist_is_refused(filters: list[str | None]) -> None:
    r = client.get("/", headers=as_login("someone@example.com"))
    assert r.status_code == 403
    assert "someone@example.com is not in ADMIN_TAILSCALE_LOGINS" in r.text


def test_empty_allowlist_refuses_everyone(filters: list[str | None], monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(admin, "settings", replace(admin.settings, admin_logins=frozenset()))
    assert client.get("/", headers=as_login(OWNER)).status_code == 403


def test_allowlisted_login_sees_ingests_across_users(filters: list[str | None]) -> None:
    r = client.get("/?state=failed", headers=as_login("John.Maguire@gmail.com"))
    assert r.status_code == 200
    assert filters == ["failed"]
    assert f"Signed in to Tailscale as <b>{OWNER}</b>" in r.text
    assert "you@example.com" in r.text and "LLM timed out" in r.text
    assert "ollama unreachable" in r.text


def test_unknown_state_filter_is_rejected(filters: list[str | None]) -> None:
    assert client.get("/?state=nope", headers=as_login(OWNER)).status_code == 400


def test_web_app_never_reads_tailscale_identity_headers() -> None:
    """The web app listens on TCP, where anyone who reaches the port can send these headers."""
    web = Path(admin.__file__).parent.parent / "web"
    assert not any("tailscale-user" in p.read_text().lower() for p in web.rglob("*.py"))
