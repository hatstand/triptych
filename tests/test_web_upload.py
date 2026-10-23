from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from triptych.auth import Me
from triptych.models import IngestRequest
from triptych.web import app as web

SAMPLES = Path(__file__).parents[1] / "samples"


class FakeRestate:
    def __init__(self) -> None:
        self.sent: list[IngestRequest] = []

    async def workflow_send(self, _handler: Any, _key: str, req: IngestRequest) -> None:
        self.sent.append(req)


@pytest.fixture
def restate_client() -> Iterator[FakeRestate]:
    fake = FakeRestate()
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
