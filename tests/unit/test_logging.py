import io
import json
import logging
import string
from urllib.parse import quote

import pytest
import structlog
from hypothesis import given
from hypothesis import strategies as st
from telegram import Bot

from price_tracker.observability.logging import (
    TelegramTokenRedactingFilter,
    bind_request_context,
    configure_logging,
)


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
TOKEN_TAILS = st.text(alphabet=string.ascii_letters + string.digits + "_-", min_size=1, max_size=64)


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

    def test_python_telegram_bot_base_urls_are_redacted(self, stdlib_logging_state):
        configure_logging(level="DEBUG")
        Bot(token=FAKE_TOKEN)
        out = stdlib_logging_state.getvalue()
        assert FAKE_TOKEN not in out
        assert "Set Bot API URL: https://api.telegram.org/bot***" in out
        assert "Set Bot API File URL: https://api.telegram.org/file/bot***" in out

    @given(
        token_tail=TOKEN_TAILS,
        suffix=st.sampled_from(("", "/getUpdates", "?x=1", "#fragment", " ", '"', "'")),
        file_api=st.booleans(),
        escaped_slashes=st.booleans(),
        host_case=st.lists(st.booleans(), min_size=16, max_size=16),
        scheme=st.sampled_from(("http", "https", "HTTP", "HTTPS")),
    )
    def test_redacts_plain_and_json_escaped_urls_at_token_boundaries(
        self, token_tail, suffix, file_api, escaped_slashes, host_case, scheme
    ):
        host = "".join(
            char.upper() if uppercase else char
            for char, uppercase in zip("api.telegram.org", host_case, strict=True)
        )
        slash = r"\/" if escaped_slashes else "/"
        file_prefix = f"file{slash}" if file_api else ""
        token = f"123:{token_tail}"
        escaped_suffix = suffix.replace("/", slash)
        url = f"{scheme}:{slash}{slash}{host}{slash}{file_prefix}bot{token}{escaped_suffix}"
        record = logging.LogRecord("telegram", logging.WARNING, __file__, 1, "%s", (url,), None)

        TelegramTokenRedactingFilter().filter(record)

        assert token not in record.getMessage()

    @given(token_tail=TOKEN_TAILS, file_api=st.booleans())
    def test_redacts_fully_percent_encoded_urls(self, token_tail, file_api):
        token = f"123:{token_tail}"
        file_prefix = "file/" if file_api else ""
        url = quote(
            f"https://api.telegram.org/{file_prefix}bot{token}/getUpdates",
            safe=".",
        )
        record = logging.LogRecord("telegram", logging.WARNING, __file__, 1, "%s", (url,), None)

        TelegramTokenRedactingFilter().filter(record)

        assert quote(token, safe="") not in record.getMessage()

    def test_redacts_every_occurrence_in_one_record(self):
        message = (
            "https://api.telegram.org/bot123:FAKE/a https://api.telegram.org/file/bot456:FAKE/b"
        )
        record = logging.LogRecord("telegram", logging.WARNING, __file__, 1, message, (), None)

        TelegramTokenRedactingFilter().filter(record)

        assert "123:FAKE" not in record.getMessage()
        assert "456:FAKE" not in record.getMessage()

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

    def test_malformed_record_is_kept_unchanged(self):
        record = logging.LogRecord("telegram.ext", logging.WARNING, __file__, 1, "%d", ("x",), None)
        assert TelegramTokenRedactingFilter().filter(record) is True
        assert record.msg == "%d"
        assert record.args == ("x",)


@pytest.fixture
def clean_stdlib_root():
    """Run with the root logger as a fresh process has it, and restore it afterwards."""
    root = logging.getLogger()
    saved_factory = logging.getLogRecordFactory()
    saved_level = root.level
    saved_handlers = list(root.handlers)
    quiet = ("httpx", "httpcore", "apscheduler")
    saved_levels = {name: logging.getLogger(name).level for name in quiet}
    root.handlers[:] = []
    try:
        yield
    finally:
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)
        logging.setLogRecordFactory(saved_factory)
        for name, level in saved_levels.items():
            logging.getLogger(name).setLevel(level)


def _json_lines(text: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in text.strip().splitlines() if line.strip()]


class TestStdlibLoggersReachTheLogStream:
    """Almost every module logs through ``logging.getLogger``: those records must be emitted."""

    def test_info_from_a_stdlib_logger_is_emitted_as_json(self, capsys, clean_stdlib_root):
        configure_logging(level="INFO")
        logging.getLogger("price_tracker.core.scheduler").info("sweep done: %d checked", 3)
        records = _json_lines(capsys.readouterr().out)
        assert len(records) == 1
        record = records[0]
        assert record["event"] == "sweep done: 3 checked"
        assert record["level"] == "info"
        assert record["logger"] == "price_tracker.core.scheduler"
        assert str(record["timestamp"]).endswith("Z")

    def test_level_filters_stdlib_records(self, capsys, clean_stdlib_root):
        configure_logging(level="WARNING")
        logger = logging.getLogger("price_tracker.scrapers.amazon")
        logger.info("dropped")
        logger.warning("kept")
        events = [record["event"] for record in _json_lines(capsys.readouterr().out)]
        assert events == ["kept"]

    def test_configuring_twice_emits_each_record_once(self, capsys, clean_stdlib_root):
        configure_logging(level="INFO")
        configure_logging(level="INFO")
        logging.getLogger("price_tracker.main").info("once")
        events = [record["event"] for record in _json_lines(capsys.readouterr().out)]
        assert events == ["once"]

    def test_exception_is_rendered_inside_the_json_line(self, capsys, clean_stdlib_root):
        configure_logging(level="INFO")
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("price_tracker.core.scheduler").exception("check failed")
        records = _json_lines(capsys.readouterr().out)
        assert len(records) == 1
        assert records[0]["level"] == "error"
        assert "RuntimeError: boom" in str(records[0]["exception"])

    def test_token_in_a_traceback_is_redacted(self, capsys, clean_stdlib_root):
        configure_logging(level="INFO")
        try:
            raise RuntimeError(f"POST https://api.telegram.org/bot{FAKE_TOKEN}/sendMessage failed")
        except RuntimeError:
            logging.getLogger("price_tracker.notifier.telegram").exception("delivery failed")
        out = capsys.readouterr().out
        assert FAKE_TOKEN not in out
        assert "bot***" in out

    def test_job_runner_chatter_is_silenced_below_warning(self, capsys, clean_stdlib_root):
        configure_logging(level="INFO")
        logging.getLogger("apscheduler.executors.default").info("Running job digest_flush")
        logging.getLogger("apscheduler.executors.default").warning("job missed")
        events = [record["event"] for record in _json_lines(capsys.readouterr().out)]
        assert events == ["job missed"]

    def test_the_handler_follows_a_replaced_stdout(self, capsys, clean_stdlib_root):
        configure_logging(level="INFO")
        capsys.readouterr()
        with pytest.MonkeyPatch.context() as patch:
            replacement = io.StringIO()
            patch.setattr("sys.stdout", replacement)
            logging.getLogger("price_tracker.main").info("after swap")
        assert json.loads(replacement.getvalue())["event"] == "after swap"
