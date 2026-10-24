"""The browser assets that the frontend build (frontend/vite.config.ts) writes to static/: every file
name carries a content hash, so a deploy that changes a file changes its URL, and nothing (a
browser, Cloudflare's cache) can keep serving an old copy."""

import json
from pathlib import Path
from typing import Any

from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

STATIC = Path(__file__).parent / "static"
MANIFEST = Path(".vite/manifest.json")
# Vite puts every hashed file here (output names in vite.config.ts).
HASHED = "assets/"
IMMUTABLE = "public, max-age=31536000, immutable"


class Assets:
    """Looks hashed file names up in Vite's build manifest. Templates call `static('style.css')` with
    a path under frontend/src. The manifest is re-read when a rebuild replaces it (`vite build
    --watch` under Tilt)."""

    def __init__(self, static: Path = STATIC) -> None:
        self.manifest = static / MANIFEST
        self.mtime_ns: int | None = None
        self.files: dict[str, Any] = {}

    def url(self, name: str) -> str:
        try:
            mtime_ns = self.manifest.stat().st_mtime_ns
        except FileNotFoundError:  # the frontend isn't built (tests)
            return f"/static/{name}"
        if mtime_ns != self.mtime_ns:
            self.files = json.loads(self.manifest.read_bytes())
            self.mtime_ns = mtime_ns
        return f"/static/{self.files[f'src/{name}']['file']}"


class Static(StaticFiles):
    """/static, with the hashed files marked immutable so browsers and Cloudflare keep them for a
    year without asking again."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        if path.startswith(HASHED):
            response.headers["cache-control"] = IMMUTABLE
        return response
