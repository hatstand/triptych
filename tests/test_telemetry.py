import json
import logging

import pytest

from triptych import telemetry


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
    assert capsys.readouterr().out.rstrip().endswith("triptych.test WARNING plain")
