"""Running the golden set through a model: `python -m evals run`.

Each variant (a model, a prompt, thinking on or off, worked examples on or off) runs every case
through the same path an ingest takes, parsing.parse → llm.extract → enrich, against the Ollama at
OLLAMA_URL. A run is written to runs/<stamp>-<model>-<prompt version>[-think][-examples]/ as
results.json (everything, for `compare`) and summary.md (for reading).

The point is to pick what marvin should run, so the variants are models that fit marvin, run on
whatever GPU is to hand. The same tag at temperature 0 answers nearly the same on CUDA as on marvin's
ROCm (not bit for bit: the kernels differ), but not at the same speed, so durations are recorded with
the host they were measured on and only compared within one.
"""

import asyncio
import json
import re
import socket
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import ollama
from pydantic import BaseModel
from pydantic_evals import Case as EvalCase
from pydantic_evals import Dataset
from pydantic_evals.evaluators import Evaluator, EvaluatorContext
from rich.console import Console
from rich.table import Table

from evals import fewshot, score
from evals.cases import Case, Expected
from triptych import llm
from triptych.config import settings
from triptych.llm import Think
from triptych.models import Extraction, IngestSource, LlmExtractRequest


class Inputs(BaseModel):
    id: str
    path: str
    content_type: str
    source: IngestSource
    received_at: datetime


class Answer(BaseModel):
    extraction: Extraction | None = None
    error: str | None = None
    examples: list[str] = []
    """The cases shown first as worked examples (evals/fewshot.py)."""


@dataclass(frozen=True, slots=True)
class Variant:
    model: str
    prompt: str
    """"current" for production's prompt, or a file holding another."""
    think: Think = False
    examples: bool = False
    """Show each case its owner's earlier corrected cases first, as production does with LLM_EXAMPLES."""

    @property
    def system(self) -> str:
        return llm.EXTRACT_SYSTEM if self.prompt == "current" else Path(self.prompt).read_text()

    @property
    def prompt_version(self) -> str:
        return llm.prompt_version(self.system)

    @property
    def label(self) -> str:
        think = "" if self.think is False else "-think" if self.think is True else f"-think-{self.think}"
        return f"{self.model}-{self.prompt_version}{think}{'-examples' * self.examples}"


async def answer(case: Case, variant: Variant, pool: fewshot.Pool | None) -> Answer:
    """What the model makes of one case. An Ollama failure is an answer too (scored as no answer),
    so one bad case doesn't stop the run."""
    doc, data = fewshot.document(case)
    shown = await pool.examples_for(case, doc, data) if pool and variant.examples else []
    req = LlmExtractRequest(
        document=doc, received_at=case.expected.received_at, model=variant.model, examples=shown
    )
    ids = [e.ingest_id for e in shown]
    try:
        extraction = await llm.extract(req, system=variant.system, think=variant.think)
    except llm.ERRORS as e:
        return Answer(error=f"{type(e).__name__}: {e}", examples=ids)
    return Answer(extraction=extraction, examples=ids)


@dataclass
class Scored(Evaluator[Inputs, Answer, Expected]):
    """The case's scores as pydantic-evals shows them: assertions and per-case rates."""

    def evaluate(self, ctx: EvaluatorContext[Inputs, Answer, Expected]) -> dict[str, bool | float]:
        assert ctx.metadata is not None
        s = score.score_case(ctx.metadata, ctx.output.extraction, ctx.output.error)
        out: dict[str, bool | float] = {"classified": s.classified, "fully_correct": s.fully_correct}
        if s.expected_count:
            out["recall"] = len(s.pairs) / s.expected_count
        if s.predicted_count:
            out["precision"] = len(s.pairs) / s.predicted_count
        return out


def dataset(cases: list[Case]) -> Dataset[Inputs, Answer, Expected]:
    return Dataset[Inputs, Answer, Expected](
        name="extraction",
        cases=[
            EvalCase(
                name=c.id,
                inputs=Inputs(
                    id=c.id,
                    path=str(c.input),
                    content_type=c.content_type,
                    source=c.expected.source,
                    received_at=c.expected.received_at,
                ),
                metadata=c.expected,
            )
            for c in cases
        ],
        evaluators=[Scored()],
    )


def pulled_models() -> set[str]:
    try:
        tags = ollama.Client(host=settings.ollama_url, timeout=5).list()
    except llm.ERRORS as e:
        raise SystemExit(
            f"can't reach Ollama at {settings.ollama_url} ({e}); is `ollama serve` running?"
        ) from e
    return {m.model for m in tags.models if m.model}


def check_models(variants: list[Variant]) -> None:
    pulled = pulled_models()
    wanted = {v.model for v in variants} | (
        {settings.embed_model} if any(v.examples for v in variants) else set()
    )
    missing = sorted(wanted - pulled)
    if missing:
        pulls = "\n".join(f"  ollama pull {m}" for m in missing)
        raise SystemExit(f"not pulled on {settings.ollama_url}:\n{pulls}")


def git_version() -> str:
    try:
        out = subprocess.run(
            ["git", "describe", "--always", "--dirty"], capture_output=True, text=True, check=True,
            cwd=Path(__file__).parent,
        )  # fmt: skip
    except (OSError, subprocess.CalledProcessError):
        return "unknown"
    return out.stdout.strip()


def slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9.-]+", "_", text)


async def run_variant(
    variant: Variant,
    cases: list[Case],
    root: Path,
    *,
    pool: fewshot.Pool | None,
    repeat: int,
    concurrency: int,
    console: Console,
) -> Path:
    started = datetime.now(UTC)
    by_id = {c.id: c for c in cases}
    report = await dataset(cases).evaluate(
        lambda inputs: answer(by_id[inputs.id], variant, pool),
        name=variant.label,
        max_concurrency=concurrency,
        repeat=repeat,
        progress=console.is_terminal,
    )
    report.print(console=console, include_averages=True, include_durations=True)

    rows: list[dict[str, Any]] = []
    for rc in report.cases:
        case = by_id[rc.source_case_name or rc.name]
        s = score.score_case(case.expected, rc.output.extraction, rc.output.error)
        rows.append({
            "id": case.id, "run": rc.name, "tags": case.expected.tags, "duration_s": rc.task_duration,
            "output": rc.output.model_dump(mode="json"), "score": s.model_dump(mode="json"),
        })  # fmt: skip
    for f in report.failures:
        case = by_id[f.source_case_name or f.name]
        s = score.score_case(case.expected, None, f.error_message)
        rows.append({
            "id": case.id, "run": f.name, "tags": case.expected.tags, "duration_s": None,
            "output": None, "score": s.model_dump(mode="json"),
        })  # fmt: skip
    rows.sort(key=lambda r: r["run"])

    totals = score.tally([(r["tags"], score.CaseScore.model_validate(r["score"])) for r in rows])
    durations = sorted(r["duration_s"] for r in rows if r["duration_s"] is not None)
    meta = {
        "model": variant.model,
        "model_digest": await llm.model_digest(variant.model),
        "prompt": variant.prompt,
        "prompt_version": variant.prompt_version,
        "think": variant.think,
        "examples": variant.examples,
        "embed_model": settings.embed_model if variant.examples else None,
        "repeat": repeat,
        "concurrency": concurrency,
        "cases": len(cases),
        "started": started.isoformat(),
        "finished": datetime.now(UTC).isoformat(),
        "host": socket.gethostname(),
        "ollama_url": settings.ollama_url,
        "num_ctx": settings.llm_num_ctx,
        "code_version": git_version(),
        "median_duration_s": durations[len(durations) // 2] if durations else None,
    }
    out = root / "runs" / f"{started:%Y%m%d-%H%M%S}-{slug(variant.label)}"
    out.mkdir(parents=True)
    results = {"meta": meta, "summary": {t: v.summary() for t, v in totals.items()}, "cases": rows}
    (out / "results.json").write_text(json.dumps(results, indent=2) + "\n")
    (out / "summary.md").write_text(summary_md(results))
    console.print(summary_table(results))
    console.print(f"[dim]wrote {out}[/dim]")
    return out


def pct(x: float | None) -> str:
    return "–" if x is None else f"{x:.0%}"


def title(meta: dict[str, Any]) -> str:
    shown = " · examples on" if meta.get("examples") else ""
    return f"{meta['model']} · prompt {meta['prompt_version']} · think {meta['think']}{shown}"


def summary_table(results: dict[str, Any]) -> Table:
    meta, overall = results["meta"], results["summary"]["all"]
    table = Table(title=title(meta))
    table.add_column("field")
    table.add_column("accuracy", justify="right")
    for o in ("near", "wrong", "missed", "hallucinated"):
        table.add_column(o, justify="right")
    for name, f in overall["fields"].items():
        table.add_row(
            name, pct(f["accuracy"]), *(str(f.get(o, 0)) for o in ("near", "wrong", "missed", "hallucinated"))
        )
    table.caption = (
        f"fully correct {pct(overall['fully_correct'])} · classification {pct(overall['classification'])} · "
        f"recall {pct(overall['reservation_recall'])} · precision {pct(overall['reservation_precision'])} · "
        f"errors {overall['errors']}"
    )
    return table


def summary_md(results: dict[str, Any]) -> str:
    meta, summary = results["meta"], results["summary"]
    repeats = f", each run {meta['repeat']} times" if meta["repeat"] > 1 else ""
    lines = [
        f"# {title(meta)}",
        "",
        f"{meta['cases']} cases{repeats}, on {meta['host']} ({meta['ollama_url']}), "
        f"code {meta['code_version']}, model digest {(meta['model_digest'] or 'unknown')[:19]}. "
        f"Median {meta['median_duration_s'] or 0:.1f}s a case on this host (not comparable with marvin).",
        "",
        *(
            [
                f"{n} partly labelled (drafts their owners corrected) count only toward the fields they set.",
                "",
            ]
            if (n := summary["all"].get("partly_labelled"))
            else []
        ),
        "| slice | cases | fully correct | classification | recall | precision | errors |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for tag, t in summary.items():
        lines.append(
            f"| {tag} | {t['cases']} | {pct(t['fully_correct'])} | {pct(t['classification'])} | "
            f"{pct(t['reservation_recall'])} | {pct(t['reservation_precision'])} | {t['errors']} |"
        )
    lines += [
        "",
        "| field | accuracy | correct | near | wrong | missed | hallucinated |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, f in summary["all"]["fields"].items():
        counts = " | ".join(str(f.get(o, 0)) for o in ("correct", "near", "wrong", "missed", "hallucinated"))
        lines.append(f"| {name} | {pct(f['accuracy'])} | {counts} |")
    lines += ["", "## Not fully correct", ""]
    for r in results["cases"]:
        s = score.CaseScore.model_validate(r["score"])
        if s.fully_correct:
            continue
        problems = [f"error: {s.error}"] if s.error else []
        if not s.classified:
            problems.append(f"classified travel={s.predicted_travel}, expected {s.is_travel_document}")
        problems += [f"missed reservation {i}" for i in s.missed]
        problems += [f"spurious reservation {j}" for j in s.spurious]
        for p in s.pairs:
            for f in p.fields:
                if f.field in score.KEY and f.outcome not in (score.Outcome.CORRECT, score.Outcome.NA):
                    note = f" ({f.note})" if f.note else ""
                    problems.append(
                        f"{p.title}: {f.field} {f.outcome}{note}: `{f.predicted}`, expected `{f.expected}`"
                    )
        lines.append(f"- **{r['run']}**: " + "; ".join(problems))
    return "\n".join(lines) + "\n"


def run(
    cases: list[Case], variants: list[Variant], root: Path, *, repeat: int = 1, concurrency: int = 1
) -> list[Path]:
    if not cases:
        raise SystemExit("no cases to run (verified ones only, unless --include-drafts)")
    check_models(variants)
    console = Console()

    async def go() -> list[Path]:
        pool = await fewshot.pool(root) if any(v.examples for v in variants) else None
        return [
            await run_variant(
                v, cases, root, pool=pool, repeat=repeat, concurrency=concurrency, console=console
            )
            for v in variants
        ]

    return asyncio.run(go())
