from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from triptych import auth, db
from triptych.admin import app as admin
from triptych.models import ReprocessRequest

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
        allowed_origins=("https://triptych.example.com",),
        admin_origins=(SITE,),
    )
    monkeypatch.setattr(admin, "settings", settings)
    monkeypatch.setattr(admin.auth, "pending_invites", pending_invites)
    monkeypatch.setattr(admin.auth, "create_invite", create_invite)
    monkeypatch.setattr(admin.db, "ingest_counts", ingest_counts)
    monkeypatch.setattr(admin.db, "recent_ingests", recent_ingests)
    monkeypatch.setattr(admin.llm, "health", health)
    monkeypatch.setattr(admin.db, "reprocess_batches", reprocess_batches)
    monkeypatch.setattr(admin.db, "reprocess_batch", reprocess_batch)
    monkeypatch.setattr(admin.db, "correction_rates", correction_rates)
    monkeypatch.setattr(admin.db, "admin_examples", admin_examples)
    removed_examples.clear()
    monkeypatch.setattr(admin.db, "delete_example", delete_example)
    yield seen


removed_examples: list[str] = []


async def correction_rates(since: datetime) -> list[db.CorrectionRate]:
    return [
        db.CorrectionRate(
            model="gemma4:31b", prompt_version="0123456789ab", examples=True, ingests=8, corrected=1,
            edited=1, deleted=0,
        ),
        db.CorrectionRate(
            model="gemma4:31b", prompt_version="0123456789ab", examples=False, ingests=4, corrected=2,
            edited=1, deleted=1,
        ),
    ]  # fmt: skip


async def admin_examples(limit: int = 50) -> list[db.AdminExample]:
    return [
        db.AdminExample(
            ingest_id="mail-7", owner="you@example.com", label="Your Iberia flight",
            sender_domain="iberia.com",
            changes=["IB 3106: confirmation code"], partial=False,
            live_at=datetime(2026, 10, 25, 12, 10, tzinfo=UTC), shown=3,
        )
    ]  # fmt: skip


async def delete_example(ingest_id: str) -> None:
    removed_examples.append(ingest_id)


async def reprocess_batches(limit: int = 20) -> list[db.ReprocessBatch]:
    return [
        db.ReprocessBatch(
            batch_id="b1", mode="dry_run", model="gemma4:31b",
            started=datetime(2026, 10, 24, 22, 0, tzinfo=UTC),
            total=2, queued=0, failed=0, changed=1, added=1, missing=0, skipped=0,
        )
    ]  # fmt: skip


async def reprocess_batch(batch_id: str, limit: int = 200) -> list[db.ReprocessItem]:
    change = db.Change(id="r1", title="Casa", fields=[db.FieldChange(field="address", old=None, new="Rua 1")])
    diff = db.Reconciliation(changes=[change])
    return [
        db.ReprocessItem(
            id=f"{batch_id}:mail-1", ingest_id="mail-1", owner="you@example.com", label="Lisbon hotel",
            mode="dry_run", state="done", model="gemma4:31b", run_id="run-1", diff=diff, error=None,
        )
    ]  # fmt: skip


class FakeRestate:
    def __init__(self) -> None:
        self.sent: list[ReprocessRequest] = []

    async def workflow_send(self, handler: object, key: str, req: ReprocessRequest) -> None:
        assert key == req.key
        self.sent.append(req)


@pytest.fixture
def restate_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRestate]:
    fake = FakeRestate()
    added: list[tuple[str, str]] = []

    async def add_reprocess(req: ReprocessRequest, owner: str) -> None:
        added.append((req.ingest_id, owner))

    monkeypatch.setattr(admin.db, "add_reprocess", add_reprocess)
    monkeypatch.setattr(admin, "rs", lambda request: fake)
    yield fake
    assert [(r.ingest_id) for r in fake.sent] == [i for i, _ in added]  # every one is recorded first


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


def test_dashboard_shows_reprocess_batches_and_a_batch_diff(filters: list[str | None]) -> None:
    r = client.get("/?batch=b1", headers=as_login(OWNER))
    assert r.status_code == 200
    assert "gemma4:31b" in r.text and "Lisbon hotel" in r.text
    assert "address: <del>∅</del> → <ins>Rua 1</ins>" in r.text


def test_reprocess_starts_a_workflow_per_ingest_with_an_original(
    filters: list[str | None], restate_client: FakeRestate, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[dict[str, Any]] = []

    async def reprocess_candidates(**kw: Any) -> list[db.ReprocessCandidate]:
        asked.append(kw)
        return [
            db.ReprocessCandidate(ingest_id="mail-1", owner="u1", input_id="i1", current_run_id="run-1"),
            db.ReprocessCandidate(ingest_id="mail-0", owner="u1", input_id=None, current_run_id="run-0"),
        ]

    monkeypatch.setattr(admin.db, "reprocess_candidates", reprocess_candidates)
    data = {
        "action": "reprocess",
        "source": "extract",
        "model": "gemma4:31b",
        "state": "failed",
        "limit": "10",
    }
    r = client.post("/", data=data, headers={**as_login(OWNER), "Origin": SITE})
    assert r.status_code == 200
    assert "Started 1 ingest as batch" in r.text
    assert asked == [{"ingest_id": None, "state": "failed", "email": None, "since": None, "limit": 10}]
    [req] = restate_client.sent
    assert (req.ingest_id, req.mode, req.model, req.from_run_id) == ("mail-1", "dry_run", "gemma4:31b", None)

    # Re-deriving needs no original, only a stored run.
    data = {"action": "reprocess", "source": "stored", "mode": "apply", "email": "", "since": "", "state": ""}
    client.post("/", data=data, headers={**as_login(OWNER), "Origin": SITE})
    assert [(r.ingest_id, r.mode, r.from_run_id) for r in restate_client.sent[1:]] == [
        ("mail-1", "apply", "run-1"),
        ("mail-0", "apply", "run-0"),
    ]


def test_applying_a_dry_run_rederives_from_its_runs(
    filters: list[str | None], restate_client: FakeRestate, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def reprocess_runs(batch_id: str) -> list[tuple[str, str, str]]:
        assert batch_id == "b1"
        return [("mail-1", "u1", "run-1")]

    monkeypatch.setattr(admin.db, "reprocess_runs", reprocess_runs)
    r = client.post("/", data={"action": "apply", "batch": "b1"}, headers={**as_login(OWNER), "Origin": SITE})
    assert r.status_code == 200
    [req] = restate_client.sent
    assert (req.ingest_id, req.mode, req.model, req.from_run_id) == ("mail-1", "apply", None, "run-1")


@pytest.mark.parametrize(
    "data", [{"action": "reprocess", "source": "elsewhere"}, {"action": "apply"}, {"action": "drop tables"}]
)
def test_reprocess_input_is_validated(
    filters: list[str | None], restate_client: FakeRestate, data: dict[str, str]
) -> None:
    r = client.post("/", data=data, headers={**as_login(OWNER), "Origin": SITE})
    assert r.status_code in (400, 422)
    assert restate_client.sent == []


def test_reprocess_from_another_origin_is_refused(
    filters: list[str | None], restate_client: FakeRestate
) -> None:
    data = {"action": "reprocess", "source": "stored"}
    r = client.post("/", data=data, headers={**as_login(OWNER), "Origin": "https://evil.example"})
    assert r.status_code == 403
    assert restate_client.sent == []


def test_ingest_rows_offer_a_dry_run_of_just_that_ingest(
    filters: list[str | None], monkeypatch: pytest.MonkeyPatch
) -> None:
    r = client.get("/", headers=as_login(OWNER))
    assert '<input type="hidden" name="ingest" value="web-1">' not in r.text  # nothing to reprocess from

    kept = FAILED.model_copy(update={"has_original": True, "has_run": True})
    queued = FAILED.model_copy(update={"ingest_id": "web-2", "state": "queued", "has_original": True})

    async def recent_ingests(state: str | None = None, limit: int = 100) -> list[db.AdminInboxItem]:
        return [kept, queued]

    monkeypatch.setattr(admin.db, "recent_ingests", recent_ingests)
    r = client.get("/", headers=as_login(OWNER))
    assert '<input type="hidden" name="ingest" value="web-1">' in r.text
    assert 'name="source" value="extract"' in r.text and 'name="source" value="stored"' in r.text
    assert 'value="web-2"' not in r.text  # not finished yet


def test_reprocess_one_ingest(
    filters: list[str | None], restate_client: FakeRestate, monkeypatch: pytest.MonkeyPatch
) -> None:
    asked: list[dict[str, Any]] = []

    async def reprocess_candidates(**kw: Any) -> list[db.ReprocessCandidate]:
        asked.append(kw)
        return [db.ReprocessCandidate(ingest_id="web-1", owner="u1", input_id="i1", current_run_id="run-1")]

    monkeypatch.setattr(admin.db, "reprocess_candidates", reprocess_candidates)
    data = {"action": "reprocess", "ingest": "web-1", "source": "stored", "model": "gemma4:31b"}
    r = client.post("/", data=data, headers={**as_login(OWNER), "Origin": SITE})
    assert r.status_code == 200
    assert "Started 1 ingest as batch" in r.text
    assert asked == [{"ingest_id": "web-1", "state": None, "email": None, "since": None, "limit": 50}]
    [req] = restate_client.sent
    assert (req.ingest_id, req.mode, req.from_run_id) == ("web-1", "dry_run", "run-1")
    # Each go is its own batch, so the workflow key is fresh and Apply on it re-derives just this one.
    client.post("/", data=data, headers={**as_login(OWNER), "Origin": SITE})
    assert restate_client.sent[0].batch_id != restate_client.sent[1].batch_id


def test_dashboard_shows_correction_rates_and_examples(filters: list[str | None]) -> None:
    r = client.get("/", headers=as_login(OWNER))
    assert r.status_code == 200
    assert "<b>12%</b>" in r.text and "<b>50%</b>" in r.text  # 1 of 8 with examples, 2 of 4 without
    assert "Your Iberia flight" in r.text and "IB 3106: confirmation code" in r.text


def test_remove_an_example(filters: list[str | None]) -> None:
    headers = {**as_login(OWNER), "Origin": SITE}
    r = client.post("/", data={"action": "remove_example", "ingest": "mail-7"}, headers=headers)
    assert r.status_code == 200
    assert removed_examples == ["mail-7"]
    assert "Removed the example from" in r.text


def test_removing_an_example_needs_our_origin(filters: list[str | None]) -> None:
    r = client.post(
        "/", data={"action": "remove_example", "ingest": "mail-7"},
        headers={**as_login(OWNER), "Origin": "https://evil.example"},
    )  # fmt: skip
    assert r.status_code == 403
    assert removed_examples == []
