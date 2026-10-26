"""Pulling shared cases down from the eval bucket: `python -m evals sync`.

Downloads each complete case (one with a case.json; see triptych/evalset.py) into inputs/<id>/, and
for a case with no expected.yaml yet writes a draft from the answer production gave at the time, to
be corrected and marked verified. Re-running only fetches what's new or changed.

Where the owner corrected the app's answer on the trip page (case.json's corrections: fields edited,
reservations deleted), the draft starts from their answer instead, tagged `corrected`, with what they
changed in its notes. A correction made after the draft was written redrafts it if it's still as
sync left it, and is otherwise only reported, so nothing you've touched is overwritten.

Reads with its own bucket-scoped token if given (EVALS_ACCESS_KEY_ID, EVALS_SECRET_ACCESS_KEY,
EVALS_ENDPOINT_URL; a read-only one is all it needs) and otherwise the INPUTS_* one.
"""

import json
import os
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import boto3
from botocore.config import Config

from evals.cases import (
    Expected,
    ExpectedReservation,
    draft,
    ensure_layout,
    expected_path,
    read_expected,
    render_expected,
    write_expected,
)
from triptych.enrich import CODED
from triptych.evalset import PREFIX, Correction
from triptych.journeys import iata_of
from triptych.models import Extraction


def env(name: str) -> str | None:
    return os.environ.get(f"EVALS_{name}") or os.environ.get(f"INPUTS_{name}") or None


def client() -> tuple[Any, str]:
    bucket = os.environ.get("EVALS_BUCKET")
    if not bucket:
        raise SystemExit("set EVALS_BUCKET and a token that can read it (evals/README.md)")
    s3 = boto3.client(
        "s3",
        endpoint_url=env("ENDPOINT_URL"),
        aws_access_key_id=env("ACCESS_KEY_ID"),
        aws_secret_access_key=env("SECRET_ACCESS_KEY"),
        region_name="auto",
        config=Config(
            request_checksum_calculation="when_required", response_checksum_validation="when_required"
        ),
    )
    return s3, bucket


@dataclass
class Synced:
    downloaded: list[str] = field(default_factory=list)
    drafted: list[str] = field(default_factory=list)
    redrafted: list[str] = field(default_factory=list)
    """Drafts written again because the owner corrected the app since."""
    recorrected: list[str] = field(default_factory=list)
    """Cases you've verified or edited that the owner corrected since: worth another look."""
    incomplete: list[str] = field(default_factory=list)


def tags_for(case: dict[str, Any]) -> list[str]:
    """Slices a new case starts in: where it came from and what it is."""
    kind = {"message/rfc822": "eml", "application/pdf": "pdf", "text/html": "html", "text/plain": "text"}
    content_type = case.get("content_type", "")
    tags = [case.get("source") or "upload", kind.get(content_type, content_type.split("/")[0])]
    if case.get("images"):
        tags.append("images")
    return tags


# The trip page's field names (ReservationFields) where expected.yaml's differ.
RENAMED = {"start_tz": "start_timezone", "end_tz": "end_timezone"}


def as_expected(fields: dict[str, Any]) -> dict[str, Any]:
    """Reservation fields as the owner set them, in expected.yaml's terms."""
    out = {}
    for name, value in fields.items():
        if name in ("start_local", "end_local") and value:
            value = value[:16]  # YYYY-MM-DDTHH:MM
        out[RENAMED.get(name, name)] = value
    return {k: v for k, v in out.items() if k in ExpectedReservation.model_fields}


def corrected(r: ExpectedReservation, c: Correction) -> ExpectedReservation:
    fixed = ExpectedReservation.model_validate({**r.model_dump(), **as_expected(c.fields)})
    # A location the owner corrected to 'Paris (CDG)' corrects the airport too.
    for side in ("start", "end"):
        if (code := iata_of(c.fields.get(f"{side}_location"))) is not None:
            setattr(fixed, f"{side}_iata", code)
    if fixed.kind not in CODED:
        fixed.start_iata = fixed.end_iata = None
    return fixed


def apply(expected: Expected, corrections: list[Correction]) -> list[str]:
    """Make the owner's corrections to a draft of the run's answer. Returns what they changed, for
    its notes."""
    by_index = {c.reservation: c for c in corrections if c.reservation is not None}
    kept, changed = [], []
    for i, r in enumerate(expected.reservations):
        name = r.title or r.kind.value
        if (c := by_index.get(i)) is None:
            kept.append(r)
        elif c.deleted:
            changed.append(f"deleted {name}")
        else:
            kept.append(corrected(r, c))
            changed.append(f"{name}: {', '.join(c.edited)}")
    for c in corrections:
        if c.reservation is None:
            added = ExpectedReservation.model_validate(as_expected(c.fields))
            kept.append(added)
            changed.append(f"kept {added.title or added.kind.value}, which this run didn't find")
    expected.reservations = kept
    return changed


def draft_from(case: dict[str, Any]) -> Expected:
    run = case.get("run") or {}
    output = run.get("output")
    expected = draft(
        Extraction.model_validate(output) if output else None,
        datetime.fromisoformat(case["received_at"]),
        case.get("source") or "upload",
        tags_for(case),
    )
    filed = f"{run.get('model')} (prompt {run.get('prompt_version')})" if run else "nothing (no run)"
    notes = [case.get("subject") or case.get("label") or "", f"Draft from what {filed} answered."]
    if corrections := [Correction.model_validate(c) for c in case.get("corrections") or []]:
        expected.tags.append("corrected")
        notes.append(f"Corrected in the app: {'; '.join(apply(expected, corrections))}.")
    expected.notes = "\n".join(notes).strip()
    return expected


def sync(root: Path, s3: Any, bucket: str) -> Synced:
    ensure_layout(root)
    objects: dict[str, dict[str, Any]] = defaultdict(dict)
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{PREFIX}/"):
        for o in page.get("Contents", []):
            parts = o["Key"].split("/")
            if len(parts) == 3:
                objects[parts[1]][parts[2]] = o
    done = Synced()
    for case_id, files in sorted(objects.items()):
        if "case.json" not in files:
            done.incomplete.append(case_id)  # still being copied, or a copy that failed part way
            continue
        folder = root / "inputs" / case_id
        folder.mkdir(parents=True, exist_ok=True)
        case_file = folder / "case.json"
        before = json.loads(case_file.read_text()) if case_file.exists() else None
        fetched = False
        for name, o in files.items():
            local = folder / name
            # case.json is rewritten when the owner corrects the app, maybe at the same size.
            if name != "case.json" and local.exists() and local.stat().st_size == o["Size"]:
                continue
            s3.download_file(bucket, o["Key"], str(local))
            fetched = fetched or name != "case.json"
        case = json.loads(case_file.read_text())
        if fetched or case != before:
            done.downloaded.append(case_id)
        path = expected_path(root, case_id)
        if not path.exists():
            write_expected(path, draft_from(case))
            done.drafted.append(case_id)
        elif before is not None and before.get("corrections") != case.get("corrections"):
            # Redraft only what sync wrote and nobody has touched since.
            ours = render_expected(path, draft_from(before))
            if read_expected(path).status == "draft" and path.read_text() == ours:
                write_expected(path, draft_from(case))
                done.redrafted.append(case_id)
            else:
                done.recorrected.append(case_id)
    return done
