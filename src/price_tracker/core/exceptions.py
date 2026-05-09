"""Domain exceptions raised by scrapers and core modules.

BlockEvent and subclasses represent voluntary blocking by remote sites
(rate limit, WAF, CAPTCHA). They are the only exceptions that increment
the per-domain consecutive_blocks counter in HealthManager.
"""

from __future__ import annotations


class ScrapeError(Exception):
    """Base class for all scrape-related errors."""


class ParseError(ScrapeError):
    """Raised when scraper cannot extract product info from a valid response."""


class BlockEvent(ScrapeError):
    """Raised when a site explicitly blocks the request.

    Subclasses identify the kind of block. HealthManager listens to
    BlockEvent (and subclasses) to drive the quarantine state machine.
    Network timeouts, 5xx errors, and parse errors are NOT block events.
    """

    def __init__(self, message: str = "", *, url: str = "") -> None:
        super().__init__(message)
        self.url = url


class HTTPBlockStatus(BlockEvent):
    """HTTP 429 (Too Many Requests) or 403 (Forbidden) status response."""

    def __init__(self, *, status: int, url: str = "") -> None:
        self.status = status
        super().__init__(f"HTTP {status} block on {url}", url=url)


class CaptchaDetected(BlockEvent):
    """Response body contains a CAPTCHA challenge marker."""

    def __init__(self, *, marker: str, url: str = "") -> None:
        self.marker = marker
        super().__init__(f"CAPTCHA detected ({marker}) on {url}", url=url)


class WAFBlocked(BlockEvent):
    """Response body matches a known WAF challenge fingerprint."""

    def __init__(self, *, provider: str, url: str = "") -> None:
        self.provider = provider
        super().__init__(f"WAF block ({provider}) on {url}", url=url)
