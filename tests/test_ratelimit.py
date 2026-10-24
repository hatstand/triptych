from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from triptych.auth import Me
from triptych.web import app as web
from triptych.web import ratelimit


@pytest.fixture
def client() -> Iterator[TestClient]:
    # One limiter for the whole test, as the lifespan would make; the lifespan itself never runs.
    lim = ratelimit.limiter()
    web.app.dependency_overrides[web.limiter] = lambda: lim
    yield TestClient(web.app, follow_redirects=False)
    web.app.dependency_overrides.clear()


def test_login_ceremonies_are_limited_per_client(client: TestClient) -> None:
    for _ in range(ratelimit.LOGIN.amount):
        assert client.post("/auth/login/options").status_code == 200
    resp = client.post("/auth/login/options")
    assert resp.status_code == 429
    assert int(resp.headers["retry-after"]) > 0
    assert "Too many requests" in resp.json()["detail"]
    # Another address has its own allowance.
    other = TestClient(web.app, client=("203.0.113.9", 50000))
    assert other.post("/auth/login/options").status_code == 200


def test_llm_work_is_limited_per_user(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    me = Me(id="user-1", email="me@example.com")
    web.app.dependency_overrides[web.current_user] = lambda: me
    calls: list[str] = []

    class FakeRestate:
        async def workflow_send(self, *_args: object) -> None:
            calls.append("sent")

    async def add_inbox_item(*_args: object) -> None:
        pass

    monkeypatch.setattr(web.db, "add_inbox_item", add_inbox_item)
    web.app.dependency_overrides[web.rs] = FakeRestate
    for _ in range(ratelimit.ADD_PLANS.amount):
        assert client.post("/plans", data={"text": "PNR ABC123"}).status_code == 303
    assert client.post("/plans", data={"text": "PNR ABC123"}).status_code == 429
    assert len(calls) == ratelimit.ADD_PLANS.amount
    # Someone else signed in is unaffected.
    web.app.dependency_overrides[web.current_user] = lambda: Me(id="user-2", email="you@example.com")
    assert client.post("/plans", data={"text": "PNR ABC123"}).status_code == 303
