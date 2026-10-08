"""structlog configuration emitting JSON to stdout.

Usage:
    from price_tracker.observability.logging import configure_logging
    configure_logging(level="INFO")
    log = structlog.get_logger(__name__)
    log.info("event.name", request_id="...", domain="amazon.com")
"""

from __future__ import annotations

import contextlib
import logging
import re
import sys
from typing import TYPE_CHECKING, Any

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
        redacted = message
        for pattern in _TELEGRAM_TOKEN_IN_URLS:
            redacted = pattern.sub(_REDACTED_TOKEN, redacted)
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
    whatever handler formats it. Limitation: exception tracebacks are rendered
    by the handler's formatter and are not rewritten.
    """
    for name in _URL_LOGGING_LOGGERS:
        logging.getLogger(name).setLevel(logging.WARNING)
    current = logging.getLogRecordFactory()
    if not isinstance(current, _RedactingRecordFactory):
        logging.setLogRecordFactory(_RedactingRecordFactory(current))


def configure_logging(*, level: str = "INFO") -> None:
    """Configure structlog with JSON renderer to stdout.

    Also silences the URL-logging HTTP client loggers below WARNING and
    redacts the Telegram bot token from every stdlib log record.

    Idempotent — safe to call multiple times.
    """
    log_level = getattr(logging, level.upper(), logging.INFO)
    _install_token_redaction()

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
