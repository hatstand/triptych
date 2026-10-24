import json
import logging
from collections.abc import Iterator
from dataclasses import replace
from datetime import UTC, datetime
from functools import partial
from http.server import BaseHTTPRequestHandler, HTTPServer
from threading import Thread
from typing import Any, cast

import httpx
import pytest
from fastapi.testclient import TestClient
from opentelemetry import trace
from opentelemetry.sdk.trace import ReadableSpan, TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from triptych import llm, telemetry, worker
from triptych.models import Extraction, LlmExtractRequest, ParsedDocument
from triptych.web import app as web

TRACE_ID = "4bf92f3577b34da6a3ce929d0e0e4736"
TRACEPARENT = f"00-{TRACE_ID}-00f067aa0ba902b7-01"


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> Iterator[InMemorySpanExporter]:
    """Finished spans, from the process's tracer provider (set up here if nothing has yet)."""
    for var in ("OTEL_EXPORTER_OTLP_ENDPOINT", "OTEL_SDK_DISABLED"):
        monkeypatch.delenv(var, raising=False)
    telemetry.setup("triptych-test")
    provider = trace.get_tracer_provider()
    assert isinstance(provider, TracerProvider)
    exporter = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    yield exporter
    exporter.shutdown()


def named(exporter: InMemorySpanExporter, prefix: str) -> list[ReadableSpan]:
    return [s for s in exporter.get_finished_spans() if s.name.startswith(prefix)]


def test_json_log_lines_carry_trace_ids(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    telemetry.setup("triptych-test")  # after capsys has swapped stdout, so the handler writes there
    log = logging.getLogger("triptych.test")

    log.info("outside %s", "a span", extra={"ingest_id": "abc"})
    with trace.get_tracer("test").start_as_current_span("work") as span:
        log.warning("inside")
        ctx = span.get_span_context()

    outside, inside = (json.loads(line) for line in capsys.readouterr().out.splitlines()[-2:])
    assert outside["message"] == "outside a span"
    assert outside["ingest_id"] == "abc"
    assert outside["severity_text"] == "INFO"
    assert "trace_id" not in outside
    assert inside["trace_id"] == format(ctx.trace_id, "032x")
    assert inside["span_id"] == format(ctx.span_id, "016x")
    assert inside["logger"] == "triptych.test"


@pytest.fixture
def ollama(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[dict[str, str]]]:
    """A real HTTP server standing in for Ollama (httpx is instrumented at the transport, so a mock
    transport would skip it). Yields the headers of each request."""
    seen: list[dict[str, str]] = []
    reply = json.dumps(
        {
            "model": "gemma-test",
            "message": {"content": json.dumps({"is_travel_document": False, "reservations": []})},
            "done_reason": "stop",
            "prompt_eval_count": 120,
            "eval_count": 30,
        }
    ).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers["content-length"]))
            seen.append(dict(self.headers))
            self.send_response(200)
            self.send_header("content-length", str(len(reply)))
            self.end_headers()
            self.wfile.write(reply)

        def log_message(self, format: str, *args: Any) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    monkeypatch.setattr(
        llm, "settings", replace(llm.settings, ollama_url=f"http://127.0.0.1:{server.server_port}")
    )
    yield seen
    server.shutdown()


@pytest.mark.anyio
async def test_llm_call_is_a_genai_span(spans: InMemorySpanExporter, ollama: list[dict[str, str]]) -> None:
    req = LlmExtractRequest(
        document=ParsedDocument(subject="Your flight", text="LHR to LIS"), received_at=datetime.now(UTC)
    )
    await llm.extract(req)

    [span] = named(spans, "chat ")
    attrs: dict[str, Any] = dict(span.attributes or {})
    assert span.kind == trace.SpanKind.CLIENT
    assert attrs["gen_ai.operation.name"] == "chat"
    assert attrs["gen_ai.provider.name"] == "ollama"
    assert attrs["gen_ai.request.model"] == llm.settings.llm_model
    assert attrs["gen_ai.response.model"] == "gemma-test"
    assert attrs["gen_ai.usage.input_tokens"] == 120
    assert attrs["gen_ai.usage.output_tokens"] == 30
    assert attrs["gen_ai.response.finish_reasons"] == ("stop",)
    assert attrs["triptych.prompt.name"] == "extract"
    assert (
        attrs["triptych.prompt.version"]
        == llm.prompt_version()
        == llm.prompt_hash(llm.EXTRACT_SYSTEM, Extraction.model_json_schema())
    )
    # The HTTP call to Ollama is a child of the generation, and carries its trace onwards.
    [http] = [s for s in spans.get_finished_spans() if s.parent and s.parent.span_id == span.context.span_id]
    assert http.kind == trace.SpanKind.CLIENT
    [headers] = ollama
    assert format(span.context.trace_id, "032x") in headers["traceparent"]


@pytest.mark.anyio
async def test_llm_errors_mark_the_span(monkeypatch: pytest.MonkeyPatch, spans: InMemorySpanExporter) -> None:
    transport = httpx.MockTransport(lambda _: httpx.Response(404, text="model not found"))
    monkeypatch.setattr(httpx, "AsyncClient", partial(httpx.AsyncClient, transport=transport))
    with pytest.raises(llm.LlmError):
        await llm._chat([{"role": "user", "content": "hi"}], prompt="ask", system=llm.ASK_SYSTEM)
    [span] = named(spans, "chat ")
    assert span.status.status_code == trace.StatusCode.ERROR


@pytest.mark.anyio
async def test_restate_attempts_continue_the_callers_trace(spans: InMemorySpanExporter) -> None:
    seen: list[str] = []

    async def handler(scope: Any, receive: Any, send: Any) -> None:
        seen.append(format(trace.get_current_span().get_span_context().trace_id, "032x"))
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b""})

    # hypercorn and httpx type ASGI apps differently; it's the same protocol.
    app = cast(Any, worker.traced(handler))
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://worker") as client:
        await client.post("/invoke/Ingest/run", headers={"traceparent": TRACEPARENT})
        await client.get("/health")

    assert seen == [TRACE_ID, seen[1]]
    [attempt] = [s for s in spans.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
    assert format(attempt.context.trace_id, "032x") == TRACE_ID


def test_web_requests_are_server_spans(spans: InMemorySpanExporter) -> None:
    TestClient(web.app).get("/login", headers={"traceparent": TRACEPARENT})
    [span] = [s for s in spans.get_finished_spans() if s.kind == trace.SpanKind.SERVER]
    assert span.name == "GET /login"
    assert format(span.context.trace_id, "032x") == TRACE_ID


def test_logs_are_json_lines(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LOG_FORMAT", "json")
    telemetry.setup("triptych-test")  # after capsys has swapped stdout, so the handler writes there
    log = logging.getLogger("triptych.test")

    log.info("filed %d reservations", 2, extra={"ingest_id": "abc"})
    try:
        1 / 0  # noqa: B018
    except ZeroDivisionError:
        log.exception("boom")

    filed, boom = (json.loads(line) for line in capsys.readouterr().out.splitlines()[-2:])
    assert filed["message"] == "filed 2 reservations"
    assert filed["severity_text"] == "INFO"
    assert filed["logger"] == "triptych.test"
    assert filed["service"] == "triptych-test"
    assert filed["ingest_id"] == "abc"
    assert "timestamp" in filed
    assert boom["severity_text"] == "ERROR"
    assert "ZeroDivisionError" in boom["exc_info"]


def test_setup_twice_keeps_one_handler(monkeypatch: pytest.MonkeyPatch) -> None:
    telemetry.setup("triptych-test")
    telemetry.setup("triptych-test")
    ours = [h for h in logging.getLogger().handlers if isinstance(h, telemetry._StdoutHandler)]
    assert len(ours) == 1


def test_text_format(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setenv("LOG_FORMAT", "text")
    telemetry.setup("triptych-test")
    logging.getLogger("triptych.test").warning("plain")
    line = capsys.readouterr().out.rstrip()
    assert line.endswith("triptych.test WARNING [trace=0 span=0] plain")
