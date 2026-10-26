"""The golden set on disk. An eval directory (TRIPTYCH_EVAL_DIR, a checkout of the private evals
repo) holds:

    cases/<id>/expected.yaml   the hand-checked answer: committed
    inputs/<id>/               the original and its case.json, synced from the eval bucket: ignored
    runs/<run>/                results of `python -m evals run`: ignored
    expected.schema.json       what expected.yaml may say, for editors: committed

<id> is the ingest id the eval bucket names cases by (evalset.py), or `local-<sha>` for a file added
by hand. An expected.yaml can instead name its input with `input:`, relative to itself, which is how
the public smoke set (evals/smoke) points at samples/.
"""

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Annotated, Any, Literal

import yaml
from pydantic import AfterValidator, AliasChoices, BaseModel, ConfigDict, Field, WithJsonSchema

from triptych import places
from triptych.models import Extraction, IngestSource, Kind

SCHEMA_FILE = "expected.schema.json"
GITIGNORE = "inputs/\nruns/\n"

CONTENT_TYPES = {
    ".eml": "message/rfc822",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".html": "text/html",
    ".htm": "text/html",
    ".txt": "text/plain",
}


def _canonical_zone(name: str | None) -> str | None:
    """An alias (Asia/Chongqing, from a label written before the list) as its canonical zone."""
    if name is None:
        return None
    if (canonical := places.canonical_timezone(name)) is None:
        raise ValueError(f"{name} isn't an IANA timezone, like Europe/Lisbon")
    return canonical


def _zone(description: str) -> Any:
    """The zones extraction may answer with (models._timezone), so a label can't ask for one the
    model isn't allowed to write. Editors complete them from expected.schema.json."""
    return Annotated[
        str | None,
        AfterValidator(_canonical_zone),
        WithJsonSchema(
            {"anyOf": [{"type": "string", "enum": list(places.timezones())}, {"type": "null"}],
             "description": description}
        ),
    ]  # fmt: skip


StartZone = _zone("IANA timezone where it starts (start_city's)")
EndZone = _zone("IANA timezone where it ends (city's)")


class ExpectedReservation(BaseModel):
    """One reservation as it should be extracted. A field left out means it should be null."""

    model_config = ConfigDict(extra="forbid")

    kind: Kind
    start_local: str = Field(description="YYYY-MM-DDTHH:MM, local to where it starts")
    end_local: str | None = Field(None, description="YYYY-MM-DDTHH:MM, local to where it ends")
    start_iata: str | None = None
    end_iata: str | None = None
    city: str | None = Field(None, description="Where the traveller will be: arrival city for transport")
    country_code: str | None = Field(None, description="ISO 3166 alpha-2 of city")
    start_city: str | None = Field(None, description="Transport only: the city it departs from")
    start_country_code: str | None = None
    start_timezone: StartZone = None
    end_timezone: EndZone = Field(
        None,
        # Drafts written before there were two only have the city's.
        validation_alias=AliasChoices("end_timezone", "timezone"),
    )
    confirmation_code: str | None = None
    service_number: str | None = Field(
        None,
        validation_alias=AliasChoices("service_number", "flight_number"),
        description="Flight, train or sailing number",
    )
    # Shown in reports for eyeballing; not scored.
    title: str | None = None
    provider: str | None = None
    start_location: str | None = None
    end_location: str | None = None
    address: str | None = None
    seat: str | None = None
    travelers: list[str] = Field(default_factory=list)
    notes: str | None = None
    accept: dict[str, list[str | None]] = Field(
        default_factory=dict,
        description="Other answers that are also right, per field ({city: [New York City]}); null if "
        "leaving it out is fine too",
    )
    owner_set: list[str] = Field(
        default_factory=list,
        description="Fields its owner set in the app (sync writes these). Until the case is verified, "
        "they're all that's scored of it",
    )


class Expected(BaseModel):
    """cases/<id>/expected.yaml"""

    model_config = ConfigDict(extra="forbid")

    status: Literal["draft", "verified"] = Field(
        "draft", description="Only verified cases are scored; drafts are the model's answer, unchecked"
    )
    received_at: datetime = Field(description="When it was received: the model infers missing years from it")
    input: str | None = Field(None, description="The input file relative to this one (default: inputs/<id>/)")
    source: IngestSource = "upload"
    tags: list[str] = Field(default_factory=list, description="Slices to report on: rail, pdf, no-travel…")
    notes: str | None = None
    is_travel_document: bool
    reservations: list[ExpectedReservation] = Field(default_factory=list)

    @property
    def partly_labelled(self) -> bool:
        """A draft whose owner corrected some of it in the app: those fields are right, the rest is
        the model's unchecked answer."""
        return self.status == "draft" and any(r.owner_set for r in self.reservations)


@dataclass(frozen=True, slots=True)
class Case:
    id: str
    expected: Expected
    input: Path
    content_type: str


def eval_dir() -> Path:
    path = os.environ.get("TRIPTYCH_EVAL_DIR")
    if not path:
        raise SystemExit("set TRIPTYCH_EVAL_DIR to your checkout of the evals repo (evals/README.md)")
    return Path(path).expanduser()


def content_type(path: Path) -> str:
    return CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")


def expected_path(root: Path, case_id: str) -> Path:
    return root / "cases" / case_id / "expected.yaml"


def read_expected(path: Path) -> Expected:
    return Expected.model_validate(yaml.safe_load(path.read_text()))


def write_expected(path: Path, expected: Expected) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_expected(path, expected))


def render_expected(path: Path, expected: Expected) -> str:
    """The YAML `write_expected` writes to `path`."""
    # Status first, and the answer explicit even where it's the default.
    data = {"status": expected.status, **expected.model_dump(mode="json", exclude_defaults=True)}
    data["is_travel_document"] = expected.is_travel_document
    data["reservations"] = [
        {**r.model_dump(mode="json", exclude_none=True, exclude_defaults=True), "kind": r.kind.value}
        for r in expected.reservations
    ]
    schema = os.path.relpath(path.parents[2] / SCHEMA_FILE, path.parent)
    header = f"# yaml-language-server: $schema={schema}\n"
    return header + yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def write_schema(root: Path) -> None:
    (root / SCHEMA_FILE).write_text(json.dumps(Expected.model_json_schema(), indent=2) + "\n")


def ensure_layout(root: Path) -> None:
    (root / "cases").mkdir(parents=True, exist_ok=True)
    ignore = root / ".gitignore"
    if not ignore.exists():
        ignore.write_text(GITIGNORE)
    write_schema(root)


def find_input(root: Path, case_id: str, expected: Expected, yaml_path: Path) -> Path | None:
    if expected.input:
        path = (yaml_path.parent / expected.input).resolve()
        return path if path.exists() else None
    return next(iter(sorted((root / "inputs" / case_id).glob("original.*"))), None)


def load_cases(root: Path, *, include_drafts: bool = False, tags: tuple[str, ...] = ()) -> list[Case]:
    """The cases to score: verified ones, drafts partly labelled by their owner (scored on what they
    set), and other drafts if asked, with an input on disk and carrying every tag in `tags`. Raises on
    a case that doesn't parse, so mistakes surface instead of shrinking the set."""
    cases = []
    for path in sorted((root / "cases").glob("*/expected.yaml")):
        case_id = path.parent.name
        expected = read_expected(path)
        if expected.status != "verified" and not expected.partly_labelled and not include_drafts:
            continue
        if not set(tags) <= set(expected.tags):
            continue
        found = find_input(root, case_id, expected, path)
        if found is None:
            raise FileNotFoundError(f"{case_id}: no input (run `python -m evals sync`?)")
        cases.append(Case(id=case_id, expected=expected, input=found, content_type=content_type(found)))
    return cases


def draft(
    extraction: Extraction | None, received_at: datetime, source: IngestSource, tags: list[str]
) -> Expected:
    """An expected.yaml to correct, from what the model answered (no answer: an empty one)."""
    if extraction is None:
        return Expected(received_at=received_at, source=source, tags=tags, is_travel_document=False)
    keep = set(ExpectedReservation.model_fields) - {"accept", "owner_set"}
    return Expected(
        received_at=received_at,
        source=source,
        tags=tags,
        is_travel_document=extraction.is_travel_document,
        reservations=[
            ExpectedReservation.model_validate(r.model_dump(include=keep)) for r in extraction.reservations
        ],
    )
