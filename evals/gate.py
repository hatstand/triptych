"""The eval gate on marvin: `python -m evals gate`, after every deploy and every night.

Syncs the eval set, then runs it through production's Llm service (queued behind real mail, in the
gpu scope's eval lane) with production's model and prompt: once without worked examples and, with
LLM_EXAMPLES on, once with them. Two checks:

- **Regression.** The run without examples is compared case by case with the last good one
  (runs/gate.json): more than TOLERANCE cases that were fully correct and no longer are, net of
  those that now are, fails the gate. The cases compared are those in both runs, so labelling new
  ones doesn't move it. A pass becomes the new last good run; a failure leaves it, so a regression
  keeps failing until it's fixed or the baseline is reset.
- **Examples.** When examples were shown to any case and the run with them is net worse than the
  run without by more than TOLERANCE cases, examples are paused (db.pause_examples) until resumed on
  /admin. A worse run that showed none says nothing about them.

Only totals are printed: the gate's output ends up in GitHub Actions logs, and the per-case detail
(what was extracted, against what) holds people's email. That stays in runs/ on marvin.

Exits 0 when both pass, 3 when either fails, so the workflow that ran it fails and opens an issue.
"""

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from evals import cases as golden
from evals import score
from evals.run import Variant, run
from triptych import db
from triptych.config import settings

TOLERANCE = 1
"""Net cases a run may lose before it counts: at temperature 0 a case can still flip on a rerun."""
BASELINE = "gate.json"
FAILED = 3


@dataclass(frozen=True, slots=True)
class Verdict:
    ok: bool
    line: str


def correct(results: dict[str, Any]) -> dict[str, bool]:
    """Each case id: whether every run of it was fully correct."""
    out: dict[str, bool] = {}
    for r in results["cases"]:
        ok = score.CaseScore.model_validate(r["score"]).fully_correct
        out[r["id"]] = out.get(r["id"], True) and ok
    return out


def net_lost(before: dict[str, bool], after: dict[str, bool]) -> tuple[int, int, int]:
    """(lost, gained, compared) over the cases in both."""
    both = before.keys() & after.keys()
    lost = sum(before[c] and not after[c] for c in both)
    gained = sum(after[c] and not before[c] for c in both)
    return lost, gained, len(both)


def regression(baseline: dict[str, Any] | None, results: dict[str, Any]) -> Verdict:
    now = correct(results)
    if baseline is None:
        return Verdict(True, f"no last good run; this one ({sum(now.values())}/{len(now)}) becomes it")
    lost, gained, n = net_lost(baseline["correct"], now)
    line = f"vs last good run ({baseline['code_version']}): {lost} cases lost, {gained} gained of {n} in both"
    return Verdict(lost - gained <= TOLERANCE, line)


def examples_check(off: dict[str, Any], on: dict[str, Any]) -> Verdict:
    shown = on["meta"].get("shown_examples") or 0
    if not shown:
        return Verdict(True, "examples on showed no case an example: nothing to judge")
    lost, gained, n = net_lost(correct(off), correct(on))
    line = f"examples on ({shown} shown): {lost} cases lost, {gained} gained of {n} vs off"
    return Verdict(lost - gained <= TOLERANCE, line)


def totals(results: dict[str, Any]) -> str:
    t = results["summary"]["all"]
    return (
        f"{results['meta']['model']}{' + examples' if results['meta']['examples'] else ''}: "
        f"fully correct {t['fully_correct'] or 0:.0%} of {t['cases']} · "
        f"recall {t['reservation_recall'] or 0:.0%} · precision {t['reservation_precision'] or 0:.0%} · "
        f"errors {t['errors']}"
    )


def gate(root: Path, *, via_restate: str) -> int:
    found = golden.load_cases(root)
    variants = [Variant(model=settings.llm_model, prompt="current")]
    if settings.llm_examples:
        variants.append(Variant(model=settings.llm_model, prompt="current", examples=True))
    paths = run(found, variants, root, via_restate=via_restate)
    runs = [json.loads((p / "results.json").read_text()) for p in paths]
    off = runs[0]

    baseline_path = root / "runs" / BASELINE
    baseline = json.loads(baseline_path.read_text()) if baseline_path.exists() else None
    verdicts = [regression(baseline, off)]
    for results in runs:
        print(totals(results))
    if len(runs) > 1:
        verdicts.append(examples_check(off, runs[1]))
        if not verdicts[-1].ok:
            asyncio.run(pause("eval gate", verdicts[-1].line))
            verdicts.append(Verdict(False, "paused worked examples (resume them on /admin)"))
    for v in verdicts:
        print(("ok    " if v.ok else "FAIL  ") + v.line)

    if verdicts[0].ok:
        good = {"code_version": settings.code_version, "correct": correct(off)}
        baseline_path.write_text(json.dumps(good, indent=2) + "\n")
    return 0 if all(v.ok for v in verdicts) else FAILED


async def pause(by: str, reason: str) -> None:
    db.init()
    try:
        await db.pause_examples(by, reason)
    finally:
        await db.close()
