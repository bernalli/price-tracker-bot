"""Logging configuration emitting JSON lines to stdout.

Two kinds of logger exist in the code base and both end up on the same stream:
structlog loggers (``structlog.get_logger``) and standard-library loggers
(``logging.getLogger``), which most modules use.

Usage:
    from price_tracker.observability.logging import configure_logging
    configure_logging(level="INFO")
    log = structlog.get_logger(__name__)
    log.info("event.name", request_id="...", domain="amazon.com")
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any, TextIO

import structlog

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator


# Bot API URLs carry the token in the path: /bot<token> and /file/bot<token>.
# Match ordinary URLs, raw JSON-escaped slashes, and fully percent-encoded URLs.
_SLASH = r"(?:/|\\/)"
_TELEGRAM_TOKEN_IN_URLS = (
    re.compile(
        rf"(?P<prefix>https?:{_SLASH}{_SLASH}api\.telegram\.org{_SLASH}"
        rf"(?:file{_SLASH})?bot)"
        r"(?P<token>[^/\\?#\s\"']+)"
        rf"(?P<suffix>{_SLASH}|[?#\s\"']|$)",
        re.IGNORECASE,
    ),
    re.compile(
        r"(?P<prefix>https?%3a%2f%2fapi\.telegram\.org%2f(?:file%2f)?bot)"
        r"(?P<token>(?:(?!%2f|%3f|%23|[\s\"']).)+)"
        r"(?P<suffix>%2f|%3f|%23|[\s\"']|$)",
        re.IGNORECASE,
    ),
)
_REDACTED_TOKEN = r"\g<prefix>***\g<suffix>"

# Loggers that log full request URLs (and so the bot token) at INFO/DEBUG.
_URL_LOGGING_LOGGERS = ("httpx", "httpcore")

# The job runner logs two INFO lines for every run of every job; one of the
# jobs runs each minute, which would bury the application's own records.
_CHATTY_LOGGERS = ("apscheduler",)

_STDLIB_HANDLER_NAME = "price_tracker.stdout_json"


def _redact_tokens(text: str) -> str:
    for pattern in _TELEGRAM_TOKEN_IN_URLS:
        text = pattern.sub(_REDACTED_TOKEN, text)
    return text


class TelegramTokenRedactingFilter(logging.Filter):
    """Rewrite ``api.telegram.org/bot<token>/`` to ``bot***/`` in a log record.

    The message is rendered with its args first, because httpx passes the URL
    as an argument. Only records that actually contain a token are rewritten;
    they get the redacted text as ``msg`` and empty ``args``. The filter never
    drops a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """Redact the record in place and keep it."""
        try:
            message = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record must still be logged
            return True
        redacted = _redact_tokens(message)
        if redacted != message:
            record.msg = redacted
            record.args = ()
        return True


class _RedactingRecordFactory:
    """Log record factory that applies the token filter to every new record."""

    def __init__(self, inner: Callable[..., logging.LogRecord]) -> None:
        self._inner = inner
        self._filter = TelegramTokenRedactingFilter()

    def __call__(self, *args: Any, **kwargs: Any) -> logging.LogRecord:
        record = self._inner(*args, **kwargs)
        self._filter.filter(record)
        return record


def _install_token_redaction() -> None:
    """Keep the Telegram bot token out of every stdlib log record.

    A filter on the root logger would not see records propagated from child
    loggers, and a filter on the current root handlers would miss handlers
    added later (e.g. by ``logging.basicConfig``). Wrapping the record factory
    redacts each record once, at creation, whatever logger emits it and
    whatever handler formats it. Exception tracebacks are rendered later, by
    the handler's formatter: the JSON formatter installed by
    :func:`configure_logging` redacts them too, a foreign handler does not.
    """
    for name in (*_URL_LOGGING_LOGGERS, *_CHATTY_LOGGERS):
        logging.getLogger(name).setLevel(logging.WARNING)
    current = logging.getLogRecordFactory()
    if not isinstance(current, _RedactingRecordFactory):
        logging.setLogRecordFactory(_RedactingRecordFactory(current))


class _JsonFormatter(logging.Formatter):
    """Render a stdlib record with the keys the structlog renderer emits."""

    def format(self, record: logging.LogRecord) -> str:
        try:
            event = record.getMessage()
        except Exception:  # noqa: BLE001 - a malformed record must still be logged
            event = str(record.msg)
        timestamp = datetime.fromtimestamp(record.created, UTC).isoformat()
        payload: dict[str, str] = {
            "event": event,
            "level": record.levelname.lower(),
            "logger": record.name,
            "timestamp": timestamp.replace("+00:00", "Z"),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        if record.stack_info:
            payload["stack"] = self.formatStack(record.stack_info)
        return json.dumps(
            {key: _redact_tokens(value) for key, value in payload.items()},
            ensure_ascii=False,
        )


if TYPE_CHECKING:
    _TextStreamHandler = logging.StreamHandler[TextIO]
else:
    # The class is generic for type checkers only; no runtime subscript is needed.
    _TextStreamHandler = logging.StreamHandler


class _StdoutHandler(_TextStreamHandler):
    """Write to whatever ``sys.stdout`` is at emit time, not at creation time."""

    def __init__(self) -> None:
        logging.Handler.__init__(self)

    @property
    def stream(self) -> TextIO:
        return sys.stdout

    @stream.setter
    def stream(self, value: TextIO) -> None:
        """Ignore: the stream is always the current ``sys.stdout``."""


def _install_stdlib_handler(log_level: int) -> None:
    """Give standard-library loggers a handler, exactly once.

    Without one the root logger has no handler at all: Python then drops every
    record below WARNING and prints the rest bare, with no time, level or
    logger name. Most modules log through ``logging.getLogger``, so the
    scheduler, the scrapers and the notifier were silent at INFO.
    """
    root = logging.getLogger()
    for handler in list(root.handlers):
        if handler.get_name() == _STDLIB_HANDLER_NAME:
            root.removeHandler(handler)
    handler = _StdoutHandler()
    handler.set_name(_STDLIB_HANDLER_NAME)
    handler.setFormatter(_JsonFormatter())
    root.addHandler(handler)
    root.setLevel(log_level)


def configure_logging(*, level: str = "INFO") -> None:
    """Send structlog and standard-library records to stdout as JSON lines.

    Also silences the URL-logging HTTP client loggers and the job runner below
    WARNING, and redacts the Telegram bot token from every stdlib log record.

    Idempotent — safe to call multiple times.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    if not isinstance(log_level, int):
        log_level = logging.INFO
    _install_token_redaction()
    _install_stdlib_handler(log_level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stdout),
        cache_logger_on_first_use=True,
    )


@contextlib.contextmanager
def bind_request_context(**ctx: Any) -> Iterator[None]:
    """Bind contextvars for the duration of the block.

    All structlog logs inside the block carry the bound key/values.
    """
    tokens = structlog.contextvars.bind_contextvars(**ctx)
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars(*ctx.keys())
        del tokens
