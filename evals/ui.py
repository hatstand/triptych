"""A local web UI for labelling the golden set: `python -m evals ui`.

It lists the cases, shows each original beside a form of its expected answer, and saves the form to
cases/<id>/expected.yaml through the same model (cases.Expected) that `check` and `run` read it with,
so whatever it saves they accept. Beside the form is how each recorded run (runs/*/results.json) did
on the case, scored again against the label as it stands now, with a button to start the form from
that run's answer.

It serves real mail from the eval directory, so it listens on localhost only, refuses any other Host
header (DNS rebinding), and takes writes only from its own pages (Origin). Email HTML is shown in a
sandboxed frame under a CSP that runs no script and, unless asked, loads no remote images.
"""

import base64
import email
import email.policy
import hashlib
import json
import re
import webbrowser
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from email.message import EmailMessage
from pathlib import Path
from typing import Any, Self, TypedDict, cast, get_args
from zoneinfo import available_timezones

import uvicorn
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, Response
from fastapi.templating import Jinja2Templates
from pydantic import ValidationError
from starlette.datastructures import FormData
from starlette.middleware.base import RequestResponseEndpoint
from starlette.middleware.trustedhost import TrustedHostMiddleware

from evals import score
from evals.cases import (
    Expected,
    ExpectedReservation,
    content_type,
    draft,
    ensure_layout,
    expected_path,
    find_input,
    read_expected,
    write_expected,
)
from triptych import enrich, parsing
from triptych.models import Extraction, IngestRequest, IngestSource, Kind
from triptych.web.assets import STATIC, Assets, Static

HERE = Path(__file__).parent
HOSTS = ("127.0.0.1", "localhost")
CASE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")

CASE_FIELDS = ("status", "received_at", "source", "tags", "notes", "input", "is_travel_document")
SCORED = (
    "kind", "service_number", "start_local", "end_local", "start_iata", "end_iata", "start_city", "city",
    "start_country_code", "country_code", "start_timezone", "end_timezone", "confirmation_code",
)  # fmt: skip
"""The fields score.py compares, in the order the form shows them, two to a row: where it starts on
the left, where it ends on the right."""
REFERENCE = ("title", "provider", "start_location", "end_location", "address", "seat", "travelers", "notes")
"""Shown in reports only; tucked away in the form."""
RESERVATION_FIELDS = (*SCORED, "accept", *REFERENCE)
TIMES = ("start_local", "end_local")
ZONES = ("start_timezone", "end_timezone")
REMOTE_IMAGES = "https: http:"


class UiState(TypedDict):
    """What the lifespan hands every request. Read it with `state()`."""

    root: Path
    templates: Jinja2Templates
    runs: "Runs"


def state(request: Request) -> UiState:
    return cast(UiState, request.state)


# --- The form ------------------------------------------------------------------------------------


class Invalid(Exception):
    def __init__(self, errors: dict[str, str]) -> None:
        super().__init__(errors)
        self.errors = errors


def field_name(loc: tuple[int | str, ...]) -> str:
    """A pydantic error's location as the form names the input: r<i>.<field>, or the case field."""
    if loc and loc[0] == "reservations" and len(loc) >= 3:
        return f"r{loc[1]}.{loc[2]}"
    return str(loc[0]) if loc else ""


def reservation_form(r: ExpectedReservation) -> dict[str, str]:
    values = {f: "" if (v := getattr(r, f)) is None else str(v) for f in RESERVATION_FIELDS}
    values["kind"] = r.kind.value
    values["travelers"] = "\n".join(r.travelers)
    values["accept"] = yaml.safe_dump(r.accept, default_flow_style=True, allow_unicode=True).strip()
    if not r.accept:
        values["accept"] = ""
    return values


def blank_reservation() -> dict[str, str]:
    return dict.fromkeys(RESERVATION_FIELDS, "")


@dataclass
class Form:
    """An expected.yaml as the form holds it: every value a string as typed, so a form that doesn't
    validate yet can be shown again exactly as it was."""

    case: dict[str, str]
    reservations: list[dict[str, str]]
    digest: str
    """The file's digest when the form was loaded, so a save can't silently overwrite an edit made
    in another editor since."""
    errors: dict[str, str] = field(default_factory=dict)
    dirty: bool = False
    """Holds changes that aren't saved (it came back from an add, a remove or a failed save)."""

    @classmethod
    def of(cls, expected: Expected, digest: str) -> Self:
        return cls(
            case={
                "status": expected.status,
                "received_at": expected.received_at.isoformat(),
                "source": expected.source,
                "tags": ", ".join(expected.tags),
                "notes": expected.notes or "",
                "input": expected.input or "",
                "is_travel_document": "on" if expected.is_travel_document else "",
            },
            reservations=[reservation_form(r) for r in expected.reservations],
            digest=digest,
        )

    @classmethod
    def posted(cls, data: FormData) -> Self:
        def get(name: str) -> str:
            value = data.get(name)
            return value if isinstance(value, str) else ""

        count = int(get("count") or 0)
        return cls(
            case={f: get(f) for f in CASE_FIELDS},
            reservations=[{f: get(f"r{i}.{f}") for f in RESERVATION_FIELDS} for i in range(count)],
            digest=get("digest"),
        )

    def expected(self) -> Expected:
        """The form as an Expected, or Invalid with a message per input that's wrong."""
        errors: dict[str, str] = {}
        reservations: list[dict[str, Any]] = []
        for i, r in enumerate(self.reservations):
            data: dict[str, Any] = {
                f: v.strip() for f, v in r.items() if f not in ("travelers", "accept") and v.strip()
            }
            data["travelers"] = [t.strip() for t in r["travelers"].splitlines() if t.strip()]
            if r["accept"].strip():
                try:
                    data["accept"] = yaml.safe_load(r["accept"])
                except yaml.YAMLError as e:
                    errors[f"r{i}.accept"] = f"not YAML: {getattr(e, 'problem', None) or e}"
                accept = data.get("accept")
                if isinstance(accept, dict) and (unknown := sorted(set(accept) - set(score.FIELDS))):
                    errors[f"r{i}.accept"] = f"not a scored field: {', '.join(map(str, unknown))}"
            # Beyond the schema: what score.py needs to read the answer at all.
            for f in TIMES:
                if f in data and enrich.parse_local(data[f]) is None:
                    errors[f"r{i}.{f}"] = "not a time: YYYY-MM-DDTHH:MM"
            for f in ZONES:
                if f in data and not enrich.valid_tz(data[f]):
                    errors[f"r{i}.{f}"] = "not an IANA timezone"
            reservations.append(data)
        c = self.case
        raw = {
            "status": c["status"],
            "received_at": c["received_at"].strip(),
            "source": c["source"],
            "tags": [t.strip() for t in c["tags"].split(",") if t.strip()],
            "notes": c["notes"].strip() or None,
            "input": c["input"].strip() or None,
            "is_travel_document": c["is_travel_document"] == "on",
            "reservations": reservations,
        }
        expected = None
        try:
            expected = Expected.model_validate(raw)
        except ValidationError as e:
            for err in e.errors():
                errors.setdefault(field_name(err["loc"]), err["msg"])
        if errors or expected is None:
            raise Invalid(errors)
        return expected


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()[:16]


# --- Cases ---------------------------------------------------------------------------------------


@dataclass
class Listed:
    id: str
    expected: Expected | None
    problem: str | None
    subject: str | None


def subject_of(root: Path, case_id: str, expected: Expected | None, yaml_path: Path) -> str | None:
    """What a case is, for the list: the subject the eval bucket recorded, else the email's own."""
    case_json = root / "inputs" / case_id / "case.json"
    if case_json.exists():
        case = json.loads(case_json.read_text())
        if case.get("subject") or case.get("label"):
            return case.get("subject") or case.get("label")
    found = find_input(root, case_id, expected, yaml_path) if expected else None
    if found and found.suffix.lower() == ".eml":
        with found.open("rb") as f:
            subject = email.message_from_binary_file(f, policy=email.policy.default).get("subject")
        return str(subject) if subject else None
    return None


def list_cases(root: Path) -> list[Listed]:
    out = []
    for path in sorted((root / "cases").glob("*/expected.yaml")):
        case_id = path.parent.name
        try:
            expected, problem = read_expected(path), None
        except (ValidationError, ValueError, yaml.YAMLError) as e:
            expected, problem = None, str(e)
        if expected and find_input(root, case_id, expected, path) is None:
            problem = "no input (run sync?)"
        out.append(Listed(case_id, expected, problem, subject_of(root, case_id, expected, path)))
    return out


def case_path(root: Path, case_id: str) -> Path:
    path = expected_path(root, case_id)
    if not CASE_ID.fullmatch(case_id) or not path.exists():
        raise HTTPException(404, f"no case {case_id}")
    return path


def next_draft(root: Path, after: str) -> str | None:
    """The next draft after this case, going round from the start."""
    drafts = [c.id for c in list_cases(root) if c.expected and c.expected.status == "draft"]
    later = [d for d in drafts if d > after] + [d for d in drafts if d < after]
    return later[0] if later else None


# --- The original --------------------------------------------------------------------------------


def message(path: Path) -> EmailMessage:
    msg = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    assert isinstance(msg, EmailMessage)
    return msg


@dataclass
class Email:
    headers: list[tuple[str, str]]
    html: bool
    text: str | None
    attachments: list[tuple[int, str, str]]
    """(index, file name, content type)"""

    @classmethod
    def read(cls, path: Path) -> Self:
        msg = message(path)
        body = msg.get_body(preferencelist=("html", "plain"))
        html = body is not None and body.get_content_type() == "text/html"
        return cls(
            headers=[(h, str(msg[h])) for h in ("From", "To", "Date", "Subject") if msg[h]],
            html=html,
            text=None if html or body is None else body.get_content(),
            attachments=[
                (n, part.get_filename() or f"part {n + 1}", part.get_content_type())
                for n, part in enumerate(msg.iter_attachments())
            ],
        )


def model_input(found: Path, expected: Expected) -> parsing.ParsedDocument:
    """The text and images the model is given for this input, parsed as `run` parses it."""
    return parsing.parse(
        IngestRequest(
            user="eval", source=expected.source, filename=found.name, content_type=content_type(found),
            data_b64=base64.b64encode(found.read_bytes()).decode(), received_at=expected.received_at,
        )
    )  # fmt: skip


def html_csp(images: bool) -> str:
    remote = f" {REMOTE_IMAGES}" if images else ""
    return (
        f"default-src 'none'; style-src 'unsafe-inline'; img-src data:{remote}; font-src data:; "
        "sandbox allow-popups allow-popups-to-escape-sandbox"
    )


# --- Runs ----------------------------------------------------------------------------------------


@dataclass
class CaseRun:
    """One answer to this case in a recorded run, scored against the label as it is now."""

    dir: str
    index: int
    name: str
    meta: dict[str, Any]
    extraction: Extraction | None
    score: score.CaseScore


class Runs:
    """runs/*/results.json, each read again only when it changes."""

    def __init__(self, root: Path) -> None:
        self.dir = root / "runs"
        self.cache: dict[Path, tuple[int, dict[str, Any]]] = {}

    def all(self) -> dict[str, dict[str, Any]]:
        out = {}
        for path in sorted(self.dir.glob("*/results.json"), reverse=True):  # newest first
            mtime = path.stat().st_mtime_ns
            if (hit := self.cache.get(path)) is None or hit[0] != mtime:
                hit = mtime, json.loads(path.read_text())
                self.cache[path] = hit
            out[path.parent.name] = hit[1]
        return out

    def answer(self, run: str, index: int) -> tuple[dict[str, Any], Extraction | None, str | None]:
        """A run's case row, the extraction it holds (None if none or unreadable), and its error."""
        results = self.all().get(run)
        if results is None or not 0 <= index < len(results["cases"]):
            raise HTTPException(404, f"no answer {index} in run {run}")
        row = results["cases"][index]
        output = row.get("output") or {}
        error = output.get("error") or row["score"].get("error")
        try:
            extraction = Extraction.model_validate(output["extraction"]) if output.get("extraction") else None
        except ValidationError as e:  # a run from before a schema change
            extraction, error = None, f"can't read this run's answer any more: {e.error_count()} errors"
        return row, extraction, error

    def for_case(self, case_id: str, expected: Expected) -> list[CaseRun]:
        out = []
        for name, results in self.all().items():
            for index, row in enumerate(results["cases"]):
                if row["id"] != case_id:
                    continue
                row, extraction, error = self.answer(name, index)
                out.append(
                    CaseRun(
                        dir=name, index=index, name=row["run"], meta=results["meta"], extraction=extraction,
                        score=score.score_case(expected, extraction, error),
                    )
                )  # fmt: skip
        return out


# --- The app -------------------------------------------------------------------------------------


def templates_for() -> Jinja2Templates:
    # Its pages extend the web app's base.html, as the admin app's do, and use its built stylesheet.
    templates = Jinja2Templates(directory=[HERE / "templates", STATIC.parent / "templates"])
    env = cast(dict[str, Any], templates.env.globals)
    env["static"] = Assets().url
    env["kinds"] = list(Kind)
    env["sources"] = get_args(IngestSource)
    env["timezones"] = sorted(available_timezones())
    env["scored"] = SCORED
    env["reference"] = REFERENCE
    return templates


def create_app(root: Path) -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncGenerator[UiState]:
        yield {"root": root, "templates": templates_for(), "runs": Runs(root)}

    app = FastAPI(title="Triptych evals", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=list(HOSTS))
    app.mount("/static", Static(directory=STATIC, check_dir=False), name="static")

    @app.middleware("http")
    async def same_origin(request: Request, call_next: RequestResponseEndpoint) -> Response:
        """A page in another tab can post a form to localhost, so writes must come from our own pages."""
        if request.method == "POST" and request.headers.get("origin") != f"http://{request.headers['host']}":
            return Response("refusing a write from another origin", status_code=403)
        return await call_next(request)

    def render(request: Request, name: str, status_code: int = 200, **context: Any) -> HTMLResponse:
        templates = state(request)["templates"]
        return templates.TemplateResponse(request, name, context, status_code=status_code)

    def render_form(
        request: Request, case_id: str, form: Form, status_code: int = 200, **context: Any
    ) -> HTMLResponse:
        return render(request, "_form.html", status_code, case_id=case_id, form=form, **context)

    @app.get("/", response_class=HTMLResponse)
    async def index(request: Request, status: str | None = None, tag: str | None = None) -> HTMLResponse:
        root = state(request)["root"]
        everything = list_cases(root)
        counts = {
            "draft": sum(1 for c in everything if c.expected and c.expected.status == "draft"),
            "verified": sum(1 for c in everything if c.expected and c.expected.status == "verified"),
            "problem": sum(1 for c in everything if c.problem),
        }
        tags = sorted({t for c in everything if c.expected for t in c.expected.tags})

        def shown(c: Listed) -> bool:
            if status == "problem":
                return c.problem is not None
            if c.expected is None:
                return status is None and tag is None
            return status in (None, c.expected.status) and tag in (None, *c.expected.tags)

        return render(
            request, "eval_index.html", cases=[c for c in everything if shown(c)], total=len(everything),
            counts=counts, tags=tags, status=status, tag=tag, root=root,
        )  # fmt: skip

    @app.get("/cases/{case_id}", response_class=HTMLResponse)
    async def case(request: Request, case_id: str) -> HTMLResponse:
        root = state(request)["root"]
        path = case_path(root, case_id)
        ids = [c.id for c in list_cases(root)]
        at = ids.index(case_id)
        context: dict[str, Any] = {
            "case_id": case_id,
            "prev": ids[at - 1] if at > 0 else None,
            "next": ids[at + 1] if at + 1 < len(ids) else None,
            "position": (at + 1, len(ids)),
        }
        try:
            expected = read_expected(path)
        except (ValidationError, ValueError, yaml.YAMLError) as e:
            return render(request, "eval_case.html", problem=str(e), raw=path.read_text(), **context)
        return render(
            request, "eval_case.html", expected=expected, form=Form.of(expected, digest(path)),
            source=source_context(root, case_id, expected, path, "original", images=False),
            runs=state(request)["runs"].for_case(case_id, expected), **context,
        )  # fmt: skip

    @app.post("/cases/{case_id}", response_class=HTMLResponse)
    async def save(request: Request, case_id: str) -> HTMLResponse:
        root = state(request)["root"]
        path = case_path(root, case_id)
        data = await request.form()
        form = Form.posted(data)
        verify = data.get("action") == "verify"
        if verify:
            form.case["status"] = "verified"
        if form.digest != digest(path):
            form.dirty = True
            form.errors[""] = (
                "expected.yaml has changed on disk since this page loaded, so this wasn't saved. "
                "Reload to see the file as it is now."
            )
            return render_form(request, case_id, form, 409)
        try:
            expected = form.expected()
        except Invalid as e:
            form.dirty, form.errors = True, e.errors
            return render_form(request, case_id, form, 422)
        write_expected(path, expected)
        response = render_form(request, case_id, Form.of(read_expected(path), digest(path)), saved=True)
        # After the swap, so the page has the clean form before it moves on (evals.ts).
        events: dict[str, Any] = {"saved": {}}
        if verify:
            following = next_draft(root, case_id)
            events["verified"] = {"next": f"/cases/{following}" if following else "/"}
        response.headers["HX-Trigger-After-Swap"] = json.dumps(events)
        return response

    @app.post("/cases/{case_id}/form", response_class=HTMLResponse)
    async def edit_form(request: Request, case_id: str) -> HTMLResponse:
        """The form again with a reservation added, removed, or taken from a run, unsaved."""
        case_path(state(request)["root"], case_id)
        data = await request.form()
        form = Form.posted(data)
        op = str(data.get("op") or "")
        if op == "add":
            form.reservations.append(blank_reservation())
        elif op.startswith("remove:"):
            del form.reservations[int(op.removeprefix("remove:"))]
        elif op.startswith("adopt:"):
            run, _, index = op.removeprefix("adopt:").rpartition(":")
            _, extraction, _ = state(request)["runs"].answer(run, int(index))
            if extraction is None:
                raise HTTPException(400, "that run has no answer to use")
            answer = draft(extraction, datetime.now(UTC), "upload", [])
            form.case["is_travel_document"] = "on" if answer.is_travel_document else ""
            form.reservations = [reservation_form(r) for r in answer.reservations]
        form.dirty = True
        return render_form(request, case_id, form)

    @app.get("/cases/{case_id}/runs", response_class=HTMLResponse)
    async def runs(request: Request, case_id: str) -> HTMLResponse:
        expected = read_expected(case_path(state(request)["root"], case_id))
        found = state(request)["runs"].for_case(case_id, expected)
        return render(request, "_runs.html", case_id=case_id, expected=expected, runs=found)

    @app.get("/cases/{case_id}/source", response_class=HTMLResponse)
    async def source(
        request: Request, case_id: str, view: str = "original", images: bool = False
    ) -> HTMLResponse:
        root = state(request)["root"]
        path = case_path(root, case_id)
        context = source_context(root, case_id, read_expected(path), path, view, images=images)
        return render(request, "_source.html", case_id=case_id, source=context)

    def input_of(request: Request, case_id: str) -> Path:
        root = state(request)["root"]
        path = case_path(root, case_id)
        found = find_input(root, case_id, read_expected(path), path)
        if found is None:
            raise HTTPException(404, "no input (run sync?)")
        return found

    @app.get("/cases/{case_id}/original")
    async def original(request: Request, case_id: str) -> Response:
        found = input_of(request, case_id)
        response = FileResponse(found, media_type=content_type(found), content_disposition_type="inline")
        if content_type(found) == "text/html":
            response.headers["content-security-policy"] = html_csp(images=False)
        return response

    @app.get("/cases/{case_id}/body")
    async def body(request: Request, case_id: str, images: bool = False) -> Response:
        """An email's HTML body, for the sandboxed frame."""
        part = message(input_of(request, case_id)).get_body(preferencelist=("html",))
        if part is None:
            raise HTTPException(404, "no HTML body")
        return HTMLResponse(part.get_content(), headers={"content-security-policy": html_csp(images)})

    @app.get("/cases/{case_id}/part/{n}")
    async def attachment(request: Request, case_id: str, n: int) -> Response:
        parts = list(message(input_of(request, case_id)).iter_attachments())
        if not 0 <= n < len(parts):
            raise HTTPException(404, "no such attachment")
        part = parts[n]
        payload = part.get_payload(decode=True)
        ctype = part.get_content_type()
        headers = {} if ctype == "application/pdf" else {"content-security-policy": html_csp(images=False)}
        return Response(payload if isinstance(payload, bytes) else b"", media_type=ctype, headers=headers)

    return app


def source_context(
    root: Path, case_id: str, expected: Expected, yaml_path: Path, view: str, *, images: bool
) -> dict[str, Any]:
    found = find_input(root, case_id, expected, yaml_path)
    context: dict[str, Any] = {"view": view, "images": images, "found": found}
    if found is None:
        return context
    ctype = content_type(found)
    context["content_type"] = ctype
    context["name"] = found.name
    if view == "text":
        doc = model_input(found, expected)
        context["text"], context["model_images"] = doc.text, doc.images_b64
    elif ctype == "message/rfc822":
        context["email"] = Email.read(found)
    elif ctype == "text/plain":
        context["text"] = found.read_text(errors="replace")
    return context


def serve(root: Path, port: int, *, open_browser: bool = False) -> None:
    ensure_layout(root)
    if not (STATIC / ".vite/manifest.json").exists():
        print("The pages look unstyled until the frontend is built: (cd frontend && npm ci && npm run build)")
    url = f"http://127.0.0.1:{port}/"
    print(f"Labelling {root} at {url} (Ctrl+C to stop)")
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(create_app(root), host="127.0.0.1", port=port, log_level="warning")
