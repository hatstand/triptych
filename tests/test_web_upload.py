from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from triptych.auth import Me
from triptych.models import InboxItem, IngestRequest
from triptych.web import app as web

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
def restate_client(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeRestate]:
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
    client: TestClient, monkeypatch: pytest.MonkeyPatch
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
