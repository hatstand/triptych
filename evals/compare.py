"""Two runs side by side: `python -m evals compare <run> <run>`.

Shows how each headline rate and each field's accuracy moved from the first run (the baseline) to
the second, and which cases flipped between fully correct and not, so a better average can't hide a
case that broke.
"""

import json
from pathlib import Path
from typing import Any

from rich.console import Console
from rich.table import Table

from evals.run import pct
from evals.score import CaseScore

HEADLINE = ("fully_correct", "classification", "reservation_recall", "reservation_precision")


def load(path: Path) -> dict[str, Any]:
    return json.loads((path / "results.json" if path.is_dir() else path).read_text())


def delta(a: float | None, b: float | None) -> str:
    if a is None or b is None:
        return ""
    d = round((b - a) * 100)
    colour = "green" if d > 0 else "red" if d < 0 else "dim"
    return f"[{colour}]{d:+d}[/{colour}]"


def name(results: dict[str, Any]) -> str:
    m = results["meta"]
    think = "" if m["think"] is False else f" think={m['think']}"
    shown = " examples" if m.get("examples") else ""
    return f"{m['model']} {m['prompt_version']}{think}{shown}"


def fully_correct(results: dict[str, Any]) -> dict[str, bool]:
    return {r["run"]: CaseScore.model_validate(r["score"]).fully_correct for r in results["cases"]}


def compare(a_path: Path, b_path: Path, console: Console | None = None) -> None:
    console = console or Console()
    a, b = load(a_path), load(b_path)
    sa, sb = a["summary"]["all"], b["summary"]["all"]

    table = Table(title=f"{name(a)} → {name(b)}")
    for col in ("", "before", "after", "Δ pts"):
        table.add_column(col, justify="left" if not col else "right")
    for key in HEADLINE:
        table.add_row(key, pct(sa[key]), pct(sb[key]), delta(sa[key], sb[key]))
    table.add_section()
    for field in dict.fromkeys([*sa["fields"], *sb["fields"]]):
        fa = sa["fields"].get(field, {}).get("accuracy")
        fb = sb["fields"].get(field, {}).get("accuracy")
        table.add_row(field, pct(fa), pct(fb), delta(fa, fb))
    console.print(table)

    if a["meta"]["host"] == b["meta"]["host"]:
        ta, tb = a["meta"]["median_duration_s"], b["meta"]["median_duration_s"]
        if ta and tb:
            console.print(f"median {ta:.1f}s → {tb:.1f}s a case on {a['meta']['host']}")

    before, after = fully_correct(a), fully_correct(b)
    fixed = sorted(k for k in before.keys() & after.keys() if after[k] and not before[k])
    broke = sorted(k for k in before.keys() & after.keys() if before[k] and not after[k])
    only = sorted(before.keys() ^ after.keys())
    for label, cases, colour in (
        ("fixed", fixed, "green"),
        ("broke", broke, "red"),
        ("in one run only", only, "dim"),
    ):
        if cases:
            console.print(f"[{colour}]{label} ({len(cases)}):[/{colour}] " + ", ".join(cases))
