"""Logging, set up once per process (web/admin lifespan, worker and SMTP `main`).

Logging stays stdlib `logging`: modules keep `logging.getLogger(__name__)` and `extra={...}` for
structured fields. Records go to stdout as one JSON object per line, with field names from the
OpenTelemetry log data model where there is one (LOG_FORMAT=text for a plain line instead;
LOG_LEVEL sets the level). uvicorn's and hypercorn's loggers go through the same handler.
"""

import logging
import os
import sys

from pythonjsonlogger.json import JsonFormatter

from triptych.config import settings

TEXT_FORMAT = "%(asctime)s %(name)s %(levelname)s %(message)s"
JSON_FIELDS = {"levelname": "severity_text", "name": "logger"}


class _StdoutHandler(logging.StreamHandler):
    """Ours, so a second setup (tests run several lifespans) replaces it rather than doubling up,
    and leaves anyone else's handlers (pytest's capture) alone."""


def formatter(fmt: str, service: str) -> logging.Formatter:
    if fmt == "text":
        return logging.Formatter(TEXT_FORMAT)
    return JsonFormatter(
        [*JSON_FIELDS, "message"],
        rename_fields=JSON_FIELDS,
        static_fields={"service": service, "version": settings.code_version},
        timestamp=True,  # ISO 8601, UTC
        # uvicorn adds an ANSI-coloured copy of some messages.
        reserved_attrs=[*JsonFormatter().reserved_attrs, "color_message"],
    )


def setup(service: str) -> None:
    """Send this process's logs to stdout. `service` names it on every JSON line."""
    root = logging.getLogger()
    for h in [h for h in root.handlers if isinstance(h, _StdoutHandler)]:
        root.removeHandler(h)
    handler = _StdoutHandler(sys.stdout)
    handler.setFormatter(formatter(os.environ.get("LOG_FORMAT", "json").strip().lower(), service))
    root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").strip().upper())
    # uvicorn gives its loggers their own plain-text handlers; send them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True
