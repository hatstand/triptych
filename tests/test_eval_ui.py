"""The eval labelling UI (evals/ui.py) over a copy of the smoke set, with no model."""

import json
import shutil
from collections.abc import Iterator
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from evals import score
from evals.cases import ExpectedReservation, read_expected, write_expected
from evals.ui import RESERVATION_FIELDS, create_app
from triptych.models import Extraction

SMOKE = Path(__file__).resolve().parents[1] / "evals" / "smoke"
URL = "http://127.0.0.1"
WRITE = {"origin": URL}


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """The smoke set laid out as a synced eval directory: inputs/<id>/original.eml beside each case."""
    for path in sorted((SMOKE / "cases").glob("*/expected.yaml")):
        case_id, expected = path.parent.name, read_expected(path)
        assert expected.input
        (tmp_path / "inputs" / case_id).mkdir(parents=True)
        shutil.copyfile(path.parent / expected.input, tmp_path / "inputs" / case_id / "original.eml")
        expected.input = None
        if case_id == "02-hotel-lisbon":
            expected.status = "draft"
        write_expected(tmp_path / "cases" / case_id / "expected.yaml", expected)
    return tmp_path


@pytest.fixture
def client(root: Path) -> Iterator[TestClient]:
    with TestClient(create_app(root), base_url=URL) as c:
        yield c


def form_of(client: TestClient, case_id: str) -> dict[str, str]:
    """The case's form as a browser would post it, read back from the page."""
    page = BeautifulSoup(client.get(f"/cases/{case_id}").text, "html.parser")
    form = page.select_one("form#case-form")
    assert form is not None
    data: dict[str, str] = {}
    for el in form.select("input[name], textarea[name], select[name]"):
        name = str(el["name"])
        if el.name == "select":
            chosen = el.select_one("option[selected]") or el.select_one("option")
            data[name] = chosen.get_text() if chosen else ""
        elif el.name == "textarea":
            data[name] = el.get_text()
        elif el.get("type") == "checkbox":
            if el.has_attr("checked"):
                data[name] = "on"
        else:
            data[name] = str(el.get("value", ""))
    return data


def test_the_form_shows_every_field() -> None:
    assert set(RESERVATION_FIELDS) == set(ExpectedReservation.model_fields)


def test_lists_cases_and_filters_them(client: TestClient) -> None:
    page = client.get("/").text
    assert "01-flight-lisbon" in page and "Your booking is confirmed" in page
    drafts = client.get("/?status=draft").text
    assert "02-hotel-lisbon" in drafts and "01-flight-lisbon" not in drafts
    rail = client.get("/?tag=rail").text
    assert "04-rail-paris" in rail and "02-hotel-lisbon" not in rail


def test_a_case_shows_the_email_and_its_answer(client: TestClient) -> None:
    page = client.get("/cases/01-flight-lisbon").text
    assert "Atlantica Air" in page  # the From header
    assert 'value="2026-11-12T15:40"' in page
    text = client.get("/cases/01-flight-lisbon/source?view=text").text
    assert "Booking reference: QZ7K2M" in text
    assert client.get("/cases/nope").status_code == 404
    assert client.get("/cases/.git").status_code == 404


def test_email_html_is_sandboxed(client: TestClient) -> None:
    body = client.get("/cases/02-hotel-lisbon/body")
    csp = body.headers["content-security-policy"]
    assert "sandbox" in csp and "script" not in csp and "https:" not in csp
    assert (
        "https:" in client.get("/cases/02-hotel-lisbon/body?images=true").headers["content-security-policy"]
    )


def test_saving_unchanged_writes_the_same_answer(client: TestClient, root: Path) -> None:
    path = root / "cases" / "01-flight-lisbon" / "expected.yaml"
    before = read_expected(path)
    r = client.post("/cases/01-flight-lisbon", data=form_of(client, "01-flight-lisbon"), headers=WRITE)
    assert r.status_code == 200 and "Saved" in r.text
    assert read_expected(path) == before


def test_verify_saves_and_points_at_the_next_draft(client: TestClient, root: Path) -> None:
    data = form_of(client, "02-hotel-lisbon") | {"action": "verify", "r0.city": "Lisboa"}
    r = client.post("/cases/02-hotel-lisbon", data=data, headers=WRITE)
    assert r.status_code == 200
    saved = read_expected(root / "cases" / "02-hotel-lisbon" / "expected.yaml")
    assert saved.status == "verified" and saved.reservations[0].city == "Lisboa"
    # No drafts left, so the list is next.
    assert json.loads(r.headers["HX-Trigger-After-Swap"])["verified"] == {"next": "/"}


def test_an_invalid_form_is_shown_again_unsaved(client: TestClient, root: Path) -> None:
    path = root / "cases" / "01-flight-lisbon" / "expected.yaml"
    before = path.read_text()
    data = form_of(client, "01-flight-lisbon") | {
        "r0.start_local": "next Tuesday",
        "r0.end_timezone": "Lisbon",
        "r1.accept": "{citty: [x]}",
        "r1.kind": "spaceship",
    }
    r = client.post("/cases/01-flight-lisbon", data=data, headers=WRITE)
    assert r.status_code == 422
    assert (
        "not a time" in r.text and "not an IANA timezone" in r.text and "not a scored field: citty" in r.text
    )
    assert 'value="next Tuesday"' in r.text and "data-dirty" in r.text
    assert path.read_text() == before


def test_a_save_wont_overwrite_an_edit_made_elsewhere(client: TestClient, root: Path) -> None:
    data = form_of(client, "01-flight-lisbon")
    path = root / "cases" / "01-flight-lisbon" / "expected.yaml"
    edited = path.read_text().replace("Lisbon", "Lisboa", 1)
    path.write_text(edited)
    r = client.post("/cases/01-flight-lisbon", data=data, headers=WRITE)
    assert r.status_code == 409 and "changed on disk" in r.text
    assert path.read_text() == edited


def test_add_and_remove_reservations(client: TestClient) -> None:
    data = form_of(client, "03-dinner-lisbon")
    added = client.post("/cases/03-dinner-lisbon/form", data=data | {"op": "add"}, headers=WRITE).text
    assert 'name="r1.start_local"' in added and 'name="count" value="2"' in added
    removed = client.post("/cases/03-dinner-lisbon/form", data=data | {"op": "remove:0"}, headers=WRITE).text
    assert 'name="count" value="0"' in removed


def test_writes_only_from_our_own_pages(client: TestClient) -> None:
    data = form_of(client, "01-flight-lisbon")
    assert client.post("/cases/01-flight-lisbon", data=data).status_code == 403
    assert (
        client.post(
            "/cases/01-flight-lisbon", data=data, headers={"origin": "https://evil.example"}
        ).status_code
        == 403
    )
    with TestClient(client.app, base_url="http://attacker.example") as other:
        assert other.get("/").status_code == 400  # DNS rebinding: not our Host


def test_runs_are_scored_against_the_label_as_it_is(client: TestClient, root: Path) -> None:
    expected = read_expected(root / "cases" / "01-flight-lisbon" / "expected.yaml")
    answer = Extraction(
        is_travel_document=True, reservations=[score.as_extracted(r) for r in expected.reservations]
    )
    answer.reservations[1].confirmation_code = None
    run = root / "runs" / "20261026-094100-gemma4_31b-abc"
    run.mkdir(parents=True)
    row = {"id": "01-flight-lisbon", "run": "01-flight-lisbon", "tags": [], "duration_s": 1.0,
           "output": {"extraction": answer.model_dump(mode="json"), "error": None}, "score": {}}  # fmt: skip
    meta = {"model": "gemma4:31b", "prompt_version": "abc", "think": False, "started": "2026-10-26T09:41:00"}
    (run / "results.json").write_text(json.dumps({"meta": meta, "summary": {}, "cases": [row]}))

    runs = client.get("/cases/01-flight-lisbon/runs").text
    assert "gemma4:31b" in runs and "missed" in runs and "not right" in runs

    adopted = client.post(
        "/cases/01-flight-lisbon/form",
        data=form_of(client, "01-flight-lisbon") | {"op": f"adopt:{run.name}:0"},
        headers=WRITE,
    ).text
    assert 'name="r1.confirmation_code" value=""' in adopted and "data-dirty" in adopted
