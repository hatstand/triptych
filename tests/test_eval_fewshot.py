"""`python -m evals run --examples`: each case is shown what its owner had corrected before it, the
way production would have, and never anything of anyone else's. No model needed."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from evals import fewshot
from evals.cases import Expected, ExpectedReservation, expected_path, load_cases, write_expected
from evals.run import Variant, answer, examples_line
from triptych import examples, llm
from triptych.models import Extraction, Kind, LlmExtractRequest

pytestmark = pytest.mark.anyio


def put(root: Path, case_id: str, owner: str | None, day: int, sender: str, tags: list[str]) -> None:
    folder = root / "inputs" / case_id
    folder.mkdir(parents=True)
    body = (
        f"From: {sender}\r\nSubject: Your booking {case_id}\r\n\r\nMadrid to Lisbon, booking {case_id}.\r\n"
    )
    (folder / "original.eml").write_text(body)
    if owner:
        (folder / "case.json").write_text(json.dumps({"owner": owner}))
    flight = ExpectedReservation(
        kind=Kind.FLIGHT, start_local=f"2026-11-{day:02}T09:00", title=f"{case_id} MAD→LIS"
    )
    write_expected(
        expected_path(root, case_id),
        Expected(
            status="verified", received_at=datetime(2026, 10, day, tzinfo=UTC), source="email", tags=tags,
            is_travel_document=True, reservations=[flight],
        ),
    )  # fmt: skip


@pytest.fixture
def root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    async def no_vector(subject: str | None, text: str) -> None:
        return None  # compared by words, as when Ollama can't embed

    monkeypatch.setattr(examples, "embedding", no_vector)
    put(tmp_path, "mine-before", "me", 1, "Iberia <hi@iberia.com>", ["email", "corrected"])
    put(tmp_path, "mine-uncorrected", "me", 2, "Iberia <hi@iberia.com>", ["email"])
    put(tmp_path, "theirs-before", "them", 3, "Iberia <hi@iberia.com>", ["email", "corrected"])
    put(tmp_path, "local-before", None, 4, "Iberia <hi@iberia.com>", ["email", "corrected"])
    put(tmp_path, "this", "me", 5, "Iberia <news@iberia.com>", ["email"])
    put(tmp_path, "mine-after", "me", 6, "Iberia <hi@iberia.com>", ["email", "corrected"])
    return tmp_path


def case(root: Path, case_id: str) -> Any:
    return next(c for c in load_cases(root) if c.id == case_id)


async def test_a_case_is_shown_only_its_owners_earlier_corrections(root: Path) -> None:
    pool = await fewshot.pool(root)
    this = case(root, "this")
    doc, data = fewshot.document(this)
    [shown] = await pool.examples_for(this, doc, data)
    assert shown.ingest_id == "mine-before"
    assert shown.answer.reservations[0].start_local == "2026-11-01T09:00"  # the label, not a model's answer
    before = case(root, "mine-before")
    assert await pool.examples_for(before, *fewshot.document(before)) == []  # nothing earlier, not itself


async def test_an_examples_run_records_what_each_case_was_shown(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[LlmExtractRequest] = []

    async def extract(req: LlmExtractRequest, **_: Any) -> Extraction:
        sent.append(req)
        return Extraction(is_travel_document=False)

    monkeypatch.setattr(llm, "extract", extract)
    pool = await fewshot.pool(root)
    on, off = Variant(model="m", prompt="current", examples=True), Variant(model="m", prompt="current")
    assert (await answer(case(root, "this"), on, pool)).examples == ["mine-before"]
    assert (await answer(case(root, "this"), off, pool)).examples == []
    assert [len(r.examples) for r in sent] == [1, 0]
    assert on.label.endswith("-examples") and not off.label.endswith("-examples")


async def test_a_run_says_how_many_cases_were_shown_examples(root: Path) -> None:
    pool = await fewshot.pool(root)
    assert pool.size == 3  # the corrected ones with an owner: mine-before, theirs-before, mine-after

    def results(shown: int | None, rows: list[list[str]]) -> dict[str, Any]:
        meta = {"examples": shown is not None, "shown_examples": shown, "example_pool": pool.size}
        return {"meta": meta, "cases": [{"output": {"examples": e}} for e in rows]}

    assert examples_line(results(None, [[]])) is None  # examples off
    assert (
        examples_line(results(1, [["mine-before"], []]))
        == "1 of 2 shown worked examples, from 3 corrected cases"
    )
    nothing = examples_line(results(0, [[], []]))
    assert nothing is not None and nothing.endswith("(so examples on changed nothing)")
