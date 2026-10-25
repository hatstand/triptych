"""Pulling shared cases down from the eval bucket: `python -m evals sync`.

Downloads each complete case (one with a case.json; see triptych/evalset.py) into inputs/<id>/, and
for a case with no expected.yaml yet writes a draft from the answer production gave at the time, to
be corrected and marked verified. Re-running only fetches what's new or changed.

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

from evals.cases import Expected, draft, ensure_layout, expected_path, write_expected
from triptych.evalset import PREFIX
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
    incomplete: list[str] = field(default_factory=list)


def tags_for(case: dict[str, Any]) -> list[str]:
    """Slices a new case starts in: where it came from and what it is."""
    kind = {"message/rfc822": "eml", "application/pdf": "pdf", "text/html": "html", "text/plain": "text"}
    content_type = case.get("content_type", "")
    tags = [case.get("source") or "upload", kind.get(content_type, content_type.split("/")[0])]
    if case.get("images"):
        tags.append("images")
    return tags


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
    expected.notes = (
        f"{case.get('subject') or case.get('label') or ''}\nDraft from what {filed} answered.".strip()
    )
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
        fetched = False
        for name, o in files.items():
            local = folder / name
            if local.exists() and local.stat().st_size == o["Size"]:
                continue
            s3.download_file(bucket, o["Key"], str(local))
            fetched = True
        if fetched:
            done.downloaded.append(case_id)
        path = expected_path(root, case_id)
        if not path.exists():
            write_expected(path, draft_from(json.loads((folder / "case.json").read_text())))
            done.drafted.append(case_id)
    return done
