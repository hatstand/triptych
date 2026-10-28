import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from triptych.auth import Me
from triptych.models import AddReservation, InboxItem, IngestRequest, ReprocessRequest
from triptych.web import app as web
from triptych.web import assets, ratelimit

SAMPLES = Path(__file__).parents[1] / "samples"


class FakeRestate:
    def __init__(self) -> None:
        self.sent: list[IngestRequest] = []
        self.keys: list[str] = []
        self.inbox: list[InboxItem] = []

    async def workflow_send(self, _handler: Any, key: str, req: IngestRequest) -> None:
        self.sent.append(req)
        self.keys.append(key)


@pytest.fixture
def restate_client(monkeypatch: pytest.MonkeyPatch, own_every_trip: None) -> Iterator[FakeRestate]:
    fake = FakeRestate()

    async def add_inbox_item(owner: str, item: InboxItem) -> None:
        assert owner == ME.id
        fake.inbox.append(item)

    monkeypatch.setattr(web.db, "add_inbox_item", add_inbox_item)
    web.app.dependency_overrides[web.rs] = lambda: fake
    yield fake
    web.app.dependency_overrides.clear()


ME = Me(id="user-1", email="me@example.com")


@pytest.fixture
def client() -> TestClient:
    # Not used as a context manager, so the lifespan (Postgres, Restate) never runs.
    web.app.dependency_overrides[web.current_user] = lambda: ME
    web.app.dependency_overrides[web.limiter] = ratelimit.limiter  # a fresh one per request: no limits
    return TestClient(web.app, follow_redirects=False)


def test_dropped_emails_each_become_an_ingest(client: TestClient, restate_client: FakeRestate) -> None:
    names = ["01-flight-lisbon.eml", "02-hotel-lisbon.eml"]
    files = [("file", (n, (SAMPLES / n).read_bytes(), "message/rfc822")) for n in names]
    resp = client.post("/plans", files=files)
    assert resp.status_code == 303
    assert [r.filename for r in restate_client.sent] == names
    assert {(r.user, r.source, r.content_type) for r in restate_client.sent} == {
        (ME.id, "upload", "message/rfc822")
    }


def test_uploads_are_in_the_inbox_before_the_workflow_runs(
    client: TestClient, restate_client: FakeRestate
) -> None:
    files = [
        ("file", ("01-flight-lisbon.eml", (SAMPLES / "01-flight-lisbon.eml").read_bytes(), "message/rfc822"))
    ]
    client.post("/plans", files=files)
    [item] = restate_client.inbox
    assert (item.ingest_id, item.label, item.state) == (
        restate_client.keys[0],
        "01-flight-lisbon.eml",
        "queued",
    )


def test_paste_still_works(client: TestClient, restate_client: FakeRestate) -> None:
    resp = client.post("/plans", data={"text": "Booking ref QZ7K2M\nmore"})
    assert resp.status_code == 303
    [req] = restate_client.sent
    assert (req.source, req.subject) == ("paste", "Booking ref QZ7K2M")


def test_oversized_file_rejects_the_whole_drop(client: TestClient, restate_client: FakeRestate) -> None:
    files = [
        ("file", ("ok.eml", b"Subject: hi\r\n\r\nbody", "message/rfc822")),
        ("file", ("huge.pdf", b"x" * (web.MAX_UPLOAD + 1), "application/pdf")),
    ]
    resp = client.post("/plans", files=files)
    assert resp.status_code == 413
    assert "huge.pdf" in resp.json()["detail"]
    assert restate_client.sent == []


def test_signed_out_drop_goes_to_login(restate_client: FakeRestate, monkeypatch: pytest.MonkeyPatch) -> None:
    async def nobody(_request: object) -> None:
        return None

    monkeypatch.setattr(web, "signed_in", nobody)
    resp = TestClient(web.app, follow_redirects=False).post(
        "/plans", files=[("file", ("a.eml", b"Subject: hi\r\n\r\nx", "message/rfc822"))]
    )
    assert resp.status_code == 303
    assert resp.headers["location"] == "/login"
    assert restate_client.sent == []


def test_trip_photo_is_served_cacheably_and_only_to_its_owner(
    client: TestClient, monkeypatch: pytest.MonkeyPatch, own_every_trip: None
) -> None:
    async def trip_photo(owner: str, trip_id: str) -> tuple[bytes, str] | None:
        return (b"jpeg", "image/jpeg") if (owner, trip_id) == (ME.id, "t1") else None

    monkeypatch.setattr(web.db, "trip_photo", trip_photo)
    resp = client.get("/trips/t1/photo?v=abc")
    assert resp.status_code == 200
    assert resp.content == b"jpeg"
    assert resp.headers["content-type"] == "image/jpeg"
    assert "immutable" in resp.headers["cache-control"]
    assert client.get("/trips/someone-elses/photo").status_code == 404


def manifest(static: Path, css: str, mtime_ns: int) -> None:
    file = static / ".vite/manifest.json"
    file.parent.mkdir(exist_ok=True)
    file.write_text(json.dumps({"src/style.css": {"file": f"assets/{css}", "src": "src/style.css"}}))
    os.utime(file, ns=(mtime_ns, mtime_ns))


def test_static_urls_are_the_hashed_files_vite_built(tmp_path: Path) -> None:
    static = assets.Assets(tmp_path)
    assert static.url("style.css") == "/static/style.css"  # not built
    manifest(tmp_path, "style-aaa.css", 1)
    assert static.url("style.css") == "/static/assets/style-aaa.css"
    manifest(tmp_path, "style-bbb.css", 2)  # vite build --watch rebuilt it
    assert static.url("style.css") == "/static/assets/style-bbb.css"
    with pytest.raises(KeyError):
        static.url("missing.ts")


def test_hashed_static_files_are_immutable(tmp_path: Path) -> None:
    (tmp_path / "assets").mkdir()
    (tmp_path / "assets/style-aaa.css").write_text("a {}")
    (tmp_path / "robots.txt").write_text("")
    client = TestClient(assets.Static(directory=tmp_path))
    resp = client.get("/assets/style-aaa.css")
    assert resp.status_code == 200
    assert resp.headers["cache-control"] == assets.IMMUTABLE
    assert "cache-control" not in client.get("/robots.txt").headers


def test_livez_answers_without_signing_in() -> None:
    # The container healthcheck that `docker rollout web` waits on before retiring the old copy.
    resp = TestClient(web.app).get("/livez")
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}


def test_saying_a_no_travel_is_a_booking_reads_it_again(
    client: TestClient, restate_client: FakeRestate, monkeypatch: pytest.MonkeyPatch
) -> None:
    marked: list[tuple[str, str]] = []

    async def mark_reclassified(owner: str, ingest_id: str) -> bool:
        marked.append((owner, ingest_id))
        return ingest_id == "mail-1"

    async def add_reprocess(*_: Any) -> None:
        pass

    monkeypatch.setattr(web.db, "mark_reclassified", mark_reclassified)
    monkeypatch.setattr(web.db, "add_reprocess", add_reprocess)
    assert client.post("/inbox/mail-2/booking").status_code == 404
    assert restate_client.sent == []
    assert client.post("/inbox/mail-1/booking").status_code == 303
    [req] = restate_client.sent
    assert marked == [(ME.id, "mail-2"), (ME.id, "mail-1")]
    assert isinstance(req, ReprocessRequest)
    assert (req.ingest_id, req.mode, req.owner_says_travel) == ("mail-1", "apply", True)


def test_adding_by_hand_says_which_email_missed_it(
    client: TestClient, restate_client: FakeRestate, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[Any] = []

    async def object_call(_handler: Any, key: str, req: Any) -> None:
        calls.append((key, req))

    monkeypatch.setattr(restate_client, "object_call", object_call, raising=False)
    form = {"kind": "flight", "title": "Back", "start_local": "2026-11-07T18:00", "travelers": "",
            "ingest_id": "mail-1", "seat": " "}  # fmt: skip
    assert client.post("/trips/t1/reservations", data=form).status_code == 303
    assert client.post("/trips/t1/reservations", data=form | {"ingest_id": ""}).status_code == 303
    (key, req), (_, unsourced) = calls
    assert isinstance(req, AddReservation) and key == ME.id
    assert (req.trip_id, req.ingest_id, req.fields.title, req.fields.seat) == ("t1", "mail-1", "Back", None)
    assert unsourced.ingest_id is None and unsourced.reservation_id != req.reservation_id
