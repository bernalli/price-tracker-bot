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


LISTING_GONE_STATUSES: frozenset[int] = frozenset({404, 410})


class ListingGone(ScrapeError):
    """A missing product page, identified by HTTP status or store-specific content.

    Not a BlockEvent (the site is not refusing us) and not an httpx error
    (scrapers that swallow ``httpx.HTTPError`` must let this one through).
    """

    def __init__(self, *, status: int, url: str = "", http_status_observed: bool = True) -> None:
        self.status = status
        self.url = url
        self.http_status_observed = http_status_observed
        evidence = f"HTTP {status}" if http_status_observed else "Product page missing"
        super().__init__(f"{evidence} listing gone on {url}")


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
    """HTTP 429 (Too Many Requests) or 403 (Forbidden) status response.

    ``retry_after`` keeps the response's ``Retry-After`` header, when it carried a
    textual one, so the scheduler can defer the store for as long as it asked.
    """

    def __init__(self, *, status: int, url: str = "", retry_after: str | None = None) -> None:
        self.status = status
        self.retry_after = retry_after if isinstance(retry_after, str) else None
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
