"""python -m evals <command>; see evals/README.md.

sync      pull shared cases from the eval bucket, drafting expected.yaml for new ones
add       add local files (a .eml saved from Gmail, a PDF) as cases, drafted by the model
check     validate every expected.yaml and say what's left to verify
run       score models, prompts and thinking settings against the verified cases
compare   two runs side by side
ui        label cases in the browser: each original beside its answer, and how runs did on it
"""

import argparse
import asyncio
import base64
import email
import email.utils
import hashlib
import shutil
import sys
from datetime import UTC, datetime
from pathlib import Path

from pydantic import ValidationError

from evals import cases as golden
from evals.cases import eval_dir, read_expected, write_expected
from triptych import llm, parsing
from triptych.config import settings
from triptych.models import Extraction, IngestRequest, IngestSource, LlmExtractRequest


def think(value: str) -> llm.Think:
    return {"off": False, "on": True}.get(value, value)


def root_of(args: argparse.Namespace) -> Path:
    return Path(args.dir).expanduser() if args.dir else eval_dir()


def cmd_run(args: argparse.Namespace) -> None:
    from evals.run import Variant, run

    root = root_of(args)
    found = golden.load_cases(root, include_drafts=args.include_drafts, tags=tuple(args.tag))
    if args.case:
        found = [c for c in found if c.id in args.case]
    variants = [
        Variant(model=m, prompt=p, think=think(t))
        for m in args.model or [settings.llm_model]
        for p in args.prompt or ["current"]
        for t in args.think or ["off"]
    ]
    run(found, variants, root, repeat=args.repeat, concurrency=args.concurrency)


def cmd_sync(args: argparse.Namespace) -> None:
    from evals.sync import client, sync

    s3, bucket = client()
    done = sync(root_of(args), s3, bucket)
    print(f"{len(done.downloaded)} downloaded, {len(done.drafted)} new drafts")
    for case_id in done.drafted:
        print(f"  draft: cases/{case_id}/expected.yaml")
    if done.incomplete:
        print(f"skipped {len(done.incomplete)} still being copied: {', '.join(done.incomplete)}")


def cmd_check(args: argparse.Namespace) -> None:
    root = root_of(args)
    golden.ensure_layout(root)
    verified, drafts, problems = 0, [], []
    for path in sorted((root / "cases").glob("*/expected.yaml")):
        case_id = path.parent.name
        try:
            expected = read_expected(path)
        except (ValidationError, ValueError) as e:
            problems.append(f"{case_id}: {e}")
            continue
        if golden.find_input(root, case_id, expected, path) is None:
            problems.append(f"{case_id}: no input (run sync?)")
        if expected.status == "verified":
            verified += 1
        else:
            drafts.append(case_id)
    print(f"{verified} verified, {len(drafts)} drafts")
    for case_id in drafts:
        print(f"  to verify: cases/{case_id}/expected.yaml")
    for p in problems:
        print(f"error: {p}", file=sys.stderr)
    if problems:
        raise SystemExit(1)


def received(path: Path, data: bytes) -> datetime:
    """When a file was received: an email's Date header, else when the file was last changed."""
    if path.suffix.lower() == ".eml":
        date = email.message_from_bytes(data).get("Date")
        if date:
            try:
                return email.utils.parsedate_to_datetime(date)
            except (TypeError, ValueError):
                pass
    return datetime.fromtimestamp(path.stat().st_mtime, UTC)


async def model_answer(
    path: Path, data: bytes, at: datetime, source: IngestSource, model: str
) -> Extraction | None:
    """What `model` makes of a file with production's prompt, or None (and why, printed) if nothing."""
    doc = parsing.parse(
        IngestRequest(
            user="eval", source=source, filename=path.name, content_type=golden.content_type(path),
            data_b64=base64.b64encode(data).decode(), received_at=at,
        )
    )  # fmt: skip
    try:
        return await llm.extract(LlmExtractRequest(document=doc, received_at=at, model=model))
    except Exception as e:  # no model is no draft, not no case
        print(f"  {path.name}: no draft from {model} ({e}); fill it in by hand")
        return None


def cmd_add(args: argparse.Namespace) -> None:
    root = root_of(args)
    golden.ensure_layout(root)
    for name in args.files:
        path = Path(name).expanduser()
        data = path.read_bytes()
        case_id = f"local-{hashlib.sha256(data).hexdigest()[:12]}"
        target = golden.expected_path(root, case_id)
        if target.exists():
            print(f"{path.name}: already there as {case_id}")
            continue
        folder = root / "inputs" / case_id
        folder.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, folder / f"original{path.suffix.lower()}")
        source: IngestSource = "email" if path.suffix.lower() == ".eml" else "upload"
        tags = [source, path.suffix.lower().lstrip("."), *args.tag]
        at = received(path, data)
        extraction = asyncio.run(model_answer(path, data, at, source, args.model))
        expected = golden.draft(extraction, at, source, tags)
        expected.notes = f"Added from {path.name}." + (
            f" Draft from what {args.model} answered." if extraction else ""
        )
        write_expected(target, expected)
        print(f"{path.name}: draft at cases/{case_id}/expected.yaml")
    print("Local inputs aren't in the bucket: keep them, or commit them beside expected.yaml with `input:`.")


def cmd_redraft(args: argparse.Namespace) -> None:
    """Draft unverified cases again with today's prompt and schema. Verified ones are never touched."""
    from evals.run import pulled_models

    if args.model not in pulled_models():
        raise SystemExit(f"not pulled on {settings.ollama_url}: ollama pull {args.model}")
    root = root_of(args)
    golden.ensure_layout(root)  # the schema editors check against changes with the format
    note = f"Redrafted from what {args.model} (prompt {llm.prompt_version()}) answered."

    async def go() -> None:
        for path in sorted((root / "cases").glob("*/expected.yaml")):
            case_id, expected = path.parent.name, read_expected(path)
            if expected.status != "draft" or (args.case and case_id not in args.case):
                continue
            found = golden.find_input(root, case_id, expected, path)
            if found is None:
                print(f"{case_id}: no input (run sync?)")
                continue
            extraction = await model_answer(
                found, found.read_bytes(), expected.received_at, expected.source, args.model
            )
            if extraction is None:
                continue
            new = golden.draft(extraction, expected.received_at, expected.source, expected.tags)
            new.input = expected.input
            new.notes = "\n".join(n for n in ((expected.notes or "").split("\n")[0], note) if n)
            write_expected(path, new)
            print(f"{case_id}: redrafted")

    asyncio.run(go())


def cmd_ui(args: argparse.Namespace) -> None:
    from evals.ui import serve

    serve(root_of(args), args.port, open_browser=args.open)


def cmd_compare(args: argparse.Namespace) -> None:
    from evals.compare import compare

    compare(Path(args.before), Path(args.after))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m evals", description=__doc__.split("\n\n")[0])
    parser.add_argument(
        "--dir", help="the eval directory (default: TRIPTYCH_EVAL_DIR); evals/smoke for the public set"
    )
    sub = parser.add_subparsers(required=True, metavar="command")

    p = sub.add_parser("run", help="score models against the verified cases")
    p.add_argument(
        "--model", action="append", help=f"Ollama model; repeat to compare (default {settings.llm_model})"
    )
    p.add_argument(
        "--prompt", action="append", help='"current" (default) or a file holding another system prompt'
    )
    p.add_argument(
        "--think", action="append", choices=["off", "on", "low", "medium", "high"], help="default off"
    )
    p.add_argument("--tag", action="append", default=[], help="only cases with this tag (all given)")
    p.add_argument("--case", action="append", help="only this case id")
    p.add_argument(
        "--repeat", type=int, default=1, help="run each case this many times, to see how stable it is"
    )
    p.add_argument(
        "--concurrency", type=int, default=1, help="cases at once (Ollama queues beyond OLLAMA_NUM_PARALLEL)"
    )
    p.add_argument("--include-drafts", action="store_true", help="score unverified drafts too")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("sync", help="pull shared cases from the eval bucket")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("check", help="validate cases and list drafts")
    p.set_defaults(func=cmd_check)

    p = sub.add_parser("add", help="add local files as cases")
    p.add_argument("files", nargs="+")
    p.add_argument("--tag", action="append", default=[])
    p.add_argument("--model", default=settings.llm_model, help="model to draft the answer with")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("redraft", help="draft unverified cases again with the current prompt and schema")
    p.add_argument(
        "--model", default=settings.llm_model, help=f"model to draft with (default {settings.llm_model})"
    )
    p.add_argument("--case", action="append", help="only this case id")
    p.set_defaults(func=cmd_redraft)

    p = sub.add_parser("ui", help="label cases in the browser, on localhost")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--open", action="store_true", help="open it in the browser")
    p.set_defaults(func=cmd_ui)

    p = sub.add_parser("compare", help="two runs side by side")
    p.add_argument("before", help="a runs/<run> folder or its results.json")
    p.add_argument("after")
    p.set_defaults(func=cmd_compare)

    args = parser.parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
