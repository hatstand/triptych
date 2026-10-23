"""URLs for /static files that change when the file does."""

import hashlib
from functools import cache
from pathlib import Path

STATIC = Path(__file__).parent / "static"


@cache
def _digest(file: Path, _mtime_ns: int) -> str:
    return hashlib.sha256(file.read_bytes()).hexdigest()[:12]


def static_url(path: str) -> str:
    """/static/<path> tagged with a hash of the file, so a deploy that changes it changes the URL and
    browsers can't keep using a cached copy. Keyed on mtime too, so edits show up in dev. Templates
    call it as `static('style.css')`."""
    file = STATIC / path
    try:
        return f"/static/{path}?v={_digest(file, file.stat().st_mtime_ns)}"
    except FileNotFoundError:  # the frontend isn't built (tests)
        return f"/static/{path}"
