from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from triptych import auth, db
from triptych.admin import app as admin

OWNER = "john.maguire@gmail.com"
SITE = "https://triptych.example.ts.net"
invites: list[tuple[str, timedelta]] = []  # what create_invite was asked for, per test
FAILED = db.AdminInboxItem(
    ingest_id="web-1", owner="you@example.com", source="upload", label="boarding pass.pdf",
    received_at=datetime(2026, 10, 23, 9, 30, tzinfo=UTC), state="failed", message="LLM timed out",
)  # fmt: skip


@pytest.fixture
def filters(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str | None]]:
    """Stub out Postgres and Ollama; yields the state filters the page asked the db for."""
    seen: list[str | None] = []
    invites.clear()

    async def ingest_counts(since: datetime) -> dict[str, int]:
        return {"failed": 2, "done": 5}

    async def recent_ingests(state: str | None = None, limit: int = 100) -> list[db.AdminInboxItem]:
        seen.append(state)
        return [FAILED]

    async def health() -> dict[str, Any]:
        raise ConnectionError("ollama unreachable")

    async def pending_invites() -> list[auth.PendingInvite]:
        return [
            auth.PendingInvite(
                email="pending@example.com",
                created_at=datetime(2026, 10, 23, 9, 0, tzinfo=UTC),
                expires_at=datetime(2026, 10, 30, 9, 0, tzinfo=UTC),
            )
        ]

    async def create_invite(email: str, ttl: timedelta = auth.INVITE_TTL) -> str:
        invites.append((email, ttl))
        return "tok3n"

    settings = replace(
        admin.settings,
        admin_logins=frozenset({OWNER}),
        restate_ui_url=None,
        public_base_url=SITE,
        allowed_origins=(SITE,),
    )
    monkeypatch.setattr(admin, "settings", settings)
    monkeypatch.setattr(admin.auth, "pending_invites", pending_invites)
    monkeypatch.setattr(admin.auth, "create_invite", create_invite)
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


def test_restate_link_only_when_configured(
    filters: list[str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    assert "Restate UI" not in client.get("/", headers=as_login(OWNER)).text
    monkeypatch.setattr(admin, "settings", replace(admin.settings, restate_ui_url="http://marvin:9070"))
    assert 'href="http://marvin:9070"' in client.get("/", headers=as_login(OWNER)).text


def test_unknown_state_filter_is_rejected(filters: list[str | None]) -> None:
    assert client.get("/?state=nope", headers=as_login(OWNER)).status_code == 400


def test_web_app_never_reads_tailscale_identity_headers() -> None:
    """The web app listens on TCP, where anyone who reaches the port can send these headers."""
    web = Path(admin.__file__).parent.parent / "web"
    assert not any("tailscale-user" in p.read_text().lower() for p in web.rglob("*.py"))


def test_dashboard_lists_pending_invites(filters: list[str | None]) -> None:
    r = client.get("/", headers=as_login(OWNER))
    assert "pending@example.com" in r.text
    assert "Create invite" in r.text


def test_invite_link_is_shown_once_created(filters: list[str | None]) -> None:
    r = client.post(
        "/", data={"email": " New@Example.com ", "days": "3"}, headers={**as_login(OWNER), "Origin": SITE}
    )
    assert r.status_code == 200
    assert invites == [("new@example.com", timedelta(days=3))]
    assert f'value="{SITE}/register/tok3n"' in r.text
    assert "<b>new@example.com</b>" in r.text


@pytest.mark.parametrize("origin", [None, "https://evil.example", "null"])
def test_invite_from_another_origin_is_refused(filters: list[str | None], origin: str | None) -> None:
    headers = as_login(OWNER) | ({"Origin": origin} if origin else {})
    r = client.post("/", data={"email": "new@example.com"}, headers=headers)
    assert r.status_code == 403
    assert invites == []


def test_invite_needs_an_allowlisted_login(filters: list[str | None]) -> None:
    headers = {**as_login("someone@example.com"), "Origin": SITE}
    assert client.post("/", data={"email": "new@example.com"}, headers=headers).status_code == 403
    assert invites == []


@pytest.mark.parametrize("data", [{"email": "not-an-address"}, {"email": "a@b.c", "days": "0"}])
def test_invite_input_is_validated(filters: list[str | None], data: dict[str, str]) -> None:
    r = client.post("/", data=data, headers={**as_login(OWNER), "Origin": SITE})
    assert r.status_code in (400, 422)
    assert invites == []
