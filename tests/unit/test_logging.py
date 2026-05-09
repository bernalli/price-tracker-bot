import json

import pytest
import structlog

from price_tracker.observability.logging import bind_request_context, configure_logging


@pytest.fixture(autouse=True)
def reset_structlog():
    yield
    structlog.reset_defaults()


class TestConfigureLogging:
    def test_emits_json_lines(self, capsys):
        configure_logging(level="INFO")
        log = structlog.get_logger("test")
        log.info("hello", foo="bar")
        out = capsys.readouterr().out.strip().splitlines()
        assert out, "no log output captured"
        rec = json.loads(out[-1])
        assert rec["event"] == "hello"
        assert rec["foo"] == "bar"
        assert rec["level"] == "info"
        assert "timestamp" in rec

    def test_filters_below_level(self, capsys):
        configure_logging(level="WARNING")
        log = structlog.get_logger("test")
        log.info("ignored")
        log.warning("kept")
        out = capsys.readouterr().out.strip().splitlines()
        events = [json.loads(line)["event"] for line in out]
        assert "ignored" not in events
        assert "kept" in events

    def test_includes_iso_utc_timestamp(self, capsys):
        configure_logging(level="INFO")
        log = structlog.get_logger("test")
        log.info("when")
        rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rec["timestamp"].endswith("Z") or "+00:00" in rec["timestamp"]


class TestBindRequestContext:
    def test_context_appears_in_subsequent_logs(self, capsys):
        configure_logging(level="INFO")
        with bind_request_context(request_id="abc-123", scraper="amazon"):
            log = structlog.get_logger("test")
            log.info("scrape.start")
        rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert rec["request_id"] == "abc-123"
        assert rec["scraper"] == "amazon"

    def test_context_is_cleared_after_block(self, capsys):
        configure_logging(level="INFO")
        with bind_request_context(request_id="abc"):
            pass
        log = structlog.get_logger("test")
        log.info("after")
        rec = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert "request_id" not in rec
