"""Logs and traces, the OpenTelemetry way, set up once per process (web/admin lifespan, worker and
SMTP `main`).

- Logging stays stdlib `logging`: modules keep `logging.getLogger(__name__)` and `extra={...}` for
  structured fields. Records go to stdout as one JSON object per line, with field names from the
  OpenTelemetry log data model where there is one, and carry the current trace and span ids
  (LOG_FORMAT=text for a plain line instead; LOG_LEVEL sets the level). uvicorn's and hypercorn's
  loggers go through the same handler. With an endpoint set they are exported as OTel logs too.
- Traces come from the OTel SDK plus auto-instrumentation for httpx (Ollama, the Restate client,
  photo lookups) and psycopg. FastAPI apps and the Restate endpoint are instrumented where they're
  built, as ASGI middleware. httpx injects W3C `traceparent` into Restate ingress calls and the
  Restate endpoint continues each attempt's trace context, so (with Restate's own tracing on, see
  README) one trace follows an upload from the browser request through Restate into the worker
  and Ollama.
- Export is OTLP over HTTP and off unless an endpoint is set. Everything is configured by the
  standard OTEL_* variables (OTEL_EXPORTER_OTLP_ENDPOINT, …_HEADERS, OTEL_SERVICE_NAME,
  OTEL_RESOURCE_ATTRIBUTES, OTEL_TRACES_SAMPLER, OTEL_SDK_DISABLED, …), which the SDK reads itself.

OTel keeps its providers process-wide (that's its API); nothing here holds state of its own.
"""

import logging
import os
import sys
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from opentelemetry import trace
from opentelemetry._logs import get_logger_provider, set_logger_provider
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
from opentelemetry.instrumentation.logging import LoggingInstrumentor
from opentelemetry.instrumentation.logging.handler import LoggingHandler
from opentelemetry.instrumentation.psycopg import PsycopgInstrumentor
from opentelemetry.sdk._logs import LoggerProvider
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_NAMESPACE, SERVICE_VERSION, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from pythonjsonlogger.json import JsonFormatter

from triptych.config import settings

TEXT_FORMAT = "%(asctime)s %(name)s %(levelname)s [trace=%(otelTraceID)s span=%(otelSpanID)s] %(message)s"
# Field names follow the OTel log data model where there is one.
JSON_FIELDS = {
    "levelname": "severity_text",
    "name": "logger",
    "otelTraceID": "trace_id",
    "otelSpanID": "span_id",
}


def _exporting(signal: str) -> bool:
    """Whether to export a signal (traces, logs): only once an OTLP endpoint is set for it, and
    not when OTEL_<SIGNAL>_EXPORTER says none."""
    if os.environ.get(f"OTEL_{signal.upper()}_EXPORTER", "otlp").strip().lower() == "none":
        return False
    return bool(
        os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT")
        or os.environ.get(f"OTEL_EXPORTER_OTLP_{signal.upper()}_ENDPOINT")
    )


def _sdk_disabled() -> bool:
    return os.environ.get("OTEL_SDK_DISABLED", "").strip().lower() == "true"


class _StdoutHandler(logging.StreamHandler):
    """Ours, so a second setup (tests run several lifespans) replaces it rather than doubling up,
    and leaves anyone else's handlers (pytest's capture) alone."""


class _OtlpHandler(LoggingHandler):
    """Turns records into OTel log records for export. Not the exporter's own complaints, though:
    exporting those would feed back into the exporter that is failing."""

    def filter(self, record: logging.LogRecord) -> bool:
        return not record.name.startswith("opentelemetry") and bool(super().filter(record))


class _JsonFormatter(JsonFormatter):
    def process_log_record(self, log_data: dict[str, Any]) -> dict[str, Any]:
        # Outside any span the ids are "0"; leave them out rather than log a fake id.
        for key in ("trace_id", "span_id"):
            if log_data.get(key) == "0":
                del log_data[key]
        return log_data


def formatter(fmt: str, service: str) -> logging.Formatter:
    if fmt == "text":
        return logging.Formatter(TEXT_FORMAT)
    return _JsonFormatter(
        [*JSON_FIELDS, "message"],
        rename_fields=JSON_FIELDS,
        static_fields={"service": service, "version": settings.code_version},
        timestamp=True,  # ISO 8601, UTC
        # Drop the rest of the OTel bookkeeping (the ids above are what matters) and uvicorn's ANSI copy.
        reserved_attrs=[
            *JsonFormatter().reserved_attrs,
            "otelServiceName",
            "otelTraceSampled",
            "color_message",
        ],
    )


def configure_logging(service: str, logger_provider: LoggerProvider | None) -> None:
    root = logging.getLogger()
    for h in [h for h in root.handlers if isinstance(h, _StdoutHandler | _OtlpHandler)]:
        root.removeHandler(h)
    handler = _StdoutHandler(sys.stdout)
    handler.setFormatter(formatter(os.environ.get("LOG_FORMAT", "json").strip().lower(), service))
    root.addHandler(handler)
    if logger_provider is not None:
        root.addHandler(_OtlpHandler(logger_provider=logger_provider))
    root.setLevel(os.environ.get("LOG_LEVEL", "INFO").strip().upper())
    # uvicorn gives its loggers their own plain-text handlers; send them through ours instead.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers.clear()
        lg.propagate = True


@dataclass(frozen=True, slots=True)
class Telemetry:
    tracer_provider: TracerProvider | None
    logger_provider: LoggerProvider | None

    def shutdown(self) -> None:
        """Flush whatever is still batched (a deploy restarts every process)."""
        if self.tracer_provider is not None:
            self.tracer_provider.shutdown()
        if self.logger_provider is not None:
            self.logger_provider.shutdown()


def setup(service: str) -> Telemetry:
    """Configure logging and tracing for this process. `service` is the default service.name
    (OTEL_SERVICE_NAME overrides it)."""
    # Every record gets the current trace and span ids (otelTraceID/otelSpanID). Export, if any,
    # is our own handler below rather than the instrumentor's, so it can be kept off its own logs.
    if not LoggingInstrumentor().is_instrumented_by_opentelemetry:
        LoggingInstrumentor().instrument(inject_trace_context=True, enable_log_auto_instrumentation=False)
    if _sdk_disabled():
        configure_logging(service, None)
        return Telemetry(None, None)

    resource = Resource.create(
        {
            SERVICE_NAME: os.environ.get("OTEL_SERVICE_NAME") or service,
            SERVICE_NAMESPACE: "triptych",
            SERVICE_VERSION: settings.code_version,
        }
    )
    # Providers are set once per process; a second setup (tests run many lifespans) keeps the
    # first's, and leaves shutting them down to whoever made them.
    owned_tracer = None
    if not isinstance(trace.get_tracer_provider(), TracerProvider):
        # Always a real one, so log lines carry trace ids even when nothing is exported.
        owned_tracer = TracerProvider(resource=resource)
        if _exporting("traces"):
            owned_tracer.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(owned_tracer)

    owned_logger = None
    current = get_logger_provider()
    if not isinstance(current, LoggerProvider) and _exporting("logs"):
        owned_logger = current = LoggerProvider(resource=resource)
        owned_logger.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
        set_logger_provider(owned_logger)
    logger_provider = current if isinstance(current, LoggerProvider) else None

    configure_logging(service, logger_provider)
    if not HTTPXClientInstrumentor().is_instrumented_by_opentelemetry:
        HTTPXClientInstrumentor().instrument()
    if not PsycopgInstrumentor().is_instrumented_by_opentelemetry:
        PsycopgInstrumentor().instrument()
    return Telemetry(owned_tracer, owned_logger)


@contextmanager
def running(service: str) -> Generator[Telemetry]:
    t = setup(service)
    try:
        yield t
    finally:
        t.shutdown()
