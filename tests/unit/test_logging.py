import io
import json
import logging

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


FAKE_TOKEN = "123:FAKE"


@pytest.fixture
def stdlib_logging_state():
    """Snapshot and restore the stdlib logging state touched by these tests."""
    root = logging.getLogger()
    saved_factory = logging.getLogRecordFactory()
    saved_root_level = root.level
    saved_root_handlers = list(root.handlers)
    saved_levels = {name: logging.getLogger(name).level for name in ("httpx", "httpcore")}
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.DEBUG)
    try:
        yield stream
    finally:
        root.removeHandler(handler)
        root.handlers[:] = saved_root_handlers
        root.setLevel(saved_root_level)
        logging.setLogRecordFactory(saved_factory)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


class TestTelegramTokenStaysOutOfStdlibLogs:
    def test_httpx_request_log_is_silenced_even_at_debug(self, stdlib_logging_state):
        configure_logging(level="DEBUG")
        logging.getLogger("httpx").info(
            'HTTP Request: %s %s "%s %d %s"',
            "POST",
            f"https://api.telegram.org/bot{FAKE_TOKEN}/getMe",
            "HTTP/1.1",
            200,
            "OK",
        )
        out = stdlib_logging_state.getvalue()
        assert "HTTP Request" not in out
        assert FAKE_TOKEN not in out

    def test_token_redacted_in_records_from_any_logger(self, stdlib_logging_state):
        configure_logging(level="INFO")
        logging.getLogger("telegram.ext").warning(
            "request failed: %s",
            f"https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage",
        )
        out = stdlib_logging_state.getvalue()
        assert FAKE_TOKEN not in out
        assert "https://api.telegram.org/bot***/sendMessage" in out

    def test_other_hosts_are_left_untouched(self, stdlib_logging_state):
        configure_logging(level="INFO")
        url = "https://example.com/bot123:KEEP/page"
        logging.getLogger("telegram.ext").warning("fetched %s", url)
        assert url in stdlib_logging_state.getvalue()

    def test_configure_logging_twice_installs_redaction_once(self, stdlib_logging_state):
        configure_logging(level="INFO")
        factory_after_first = logging.getLogRecordFactory()
        configure_logging(level="INFO")
        assert logging.getLogRecordFactory() is factory_after_first
        assert factory_after_first is not logging.LogRecord
