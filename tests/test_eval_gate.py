"""The eval gate: regressions against the last good run, and pausing examples that make things worse."""

import dataclasses
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast

import pytest
import restate

from evals import gate, score
from evals.run import Variant, queueable, queued
from triptych.models import Extraction, LlmExtractRequest, ParsedDocument

NOW = datetime(2026, 10, 26, tzinfo=UTC)


def results(correct: dict[str, bool], *, examples: bool = False, shown: int | None = None) -> dict[str, Any]:
    def case_score(ok: bool) -> dict[str, Any]:
        return score.CaseScore(
            is_travel_document=True, predicted_travel=ok, expected_count=1, predicted_count=1
        ).model_dump(mode="json")

    return {
        "meta": {"model": "gemma4", "examples": examples, "shown_examples": shown},
        "summary": {"all": {"cases": len(correct), "fully_correct": sum(correct.values()) / len(correct),
                            "reservation_recall": 1.0, "reservation_precision": 1.0, "errors": 0}},
        "cases": [{"id": c, "score": case_score(ok)} for c, ok in correct.items()],
    }  # fmt: skip


def test_a_regression_counts_only_cases_in_both_runs_net_of_gains() -> None:
    before = {"code_version": "v1", "correct": {"a": True, "b": True, "c": False, "gone": True}}
    # One lost (b) and one gained (c): no net loss. A new case (d) doesn't count either way.
    assert gate.regression(before, results({"a": True, "b": False, "c": True, "d": False})).ok
    assert gate.regression(before, results({"a": True, "b": False, "c": False})).ok  # within tolerance
    v = gate.regression(before, results({"a": False, "b": False, "c": False}))
    assert not v.ok and "2 cases lost, 0 gained of 3" in v.line
    assert gate.regression(None, results({"a": False})).ok  # the first run becomes the baseline


def test_examples_are_judged_only_when_shown() -> None:
    off = results({"a": True, "b": True, "c": True})
    worse = results({"a": False, "b": False, "c": True}, examples=True, shown=2)
    assert not gate.examples_check(off, worse).ok
    assert gate.examples_check(off, results({"a": False, "b": False, "c": True}, examples=True, shown=0)).ok


def test_gate_pauses_examples_and_keeps_the_baseline_on_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    (tmp_path / "runs").mkdir()
    good = {"code_version": "v1", "correct": {"a": True, "b": True, "c": True}}
    (tmp_path / "runs" / gate.BASELINE).write_text(json.dumps(good))
    off = results({"a": True, "b": True, "c": True})
    on = results({"a": False, "b": False, "c": True}, examples=True, shown=3)

    def run(found: list[Any], variants: list[Variant], root: Path, *, via_restate: str) -> list[Path]:
        assert via_restate == "http://restate:8080" and [v.examples for v in variants] == [False, True]
        paths = []
        for name, r in (("off", off), ("on", on)):
            (root / "runs" / name).mkdir()
            (root / "runs" / name / "results.json").write_text(json.dumps(r))
            paths.append(root / "runs" / name)
        return paths

    paused: list[tuple[str, str]] = []

    async def pause(by: str, reason: str) -> None:
        paused.append((by, reason))

    monkeypatch.setattr(gate, "run", run)
    monkeypatch.setattr(gate, "pause", pause)
    monkeypatch.setattr(gate.golden, "load_cases", lambda root: [])
    monkeypatch.setattr(gate, "settings", dataclasses.replace(gate.settings, llm_examples=True))

    assert gate.gate(tmp_path, via_restate="http://restate:8080") == gate.FAILED
    assert [by for by, _ in paused] == ["eval gate"]
    out = capsys.readouterr().out
    assert "FAIL  examples on (3 shown): 2 cases lost, 0 gained of 3 vs off" in out
    assert "ok    vs last good run (v1)" in out
    assert json.loads((tmp_path / "runs" / gate.BASELINE).read_text())["correct"] == gate.correct(off)


def test_only_productions_prompt_can_be_queued() -> None:
    assert queueable(Variant(model="m", prompt="current"))
    assert not queueable(Variant(model="m", prompt="other.txt"))
    assert not queueable(Variant(model="m", prompt="current", think=True))


@pytest.mark.anyio
async def test_queued_cases_go_through_the_gpu_scopes_eval_lane() -> None:
    calls: list[dict[str, Any]] = []

    class Scoped:
        def __init__(self, scope: str) -> None:
            self.scope = scope

        async def service_call(self, handler: Any, req: LlmExtractRequest, **kw: Any) -> Extraction:
            calls.append({"scope": self.scope, **kw})
            return Extraction(is_travel_document=False)

    class Client:
        def scope(self, scope: str) -> Scoped:
            return Scoped(scope)

    extract = queued(cast(restate.RestateClient, Client()))
    req = LlmExtractRequest(document=ParsedDocument(subject=None, text="hi"), received_at=NOW)
    await extract(req, Variant(model="m", prompt="current"))
    await extract(req, Variant(model="m", prompt="current"))
    [first, second] = calls
    assert first["scope"] == "gpu" and first["limit_key"].startswith("eval/")
    assert first["idempotency_key"] == second["idempotency_key"]  # the same case attaches, not reruns
