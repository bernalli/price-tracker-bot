"""Abstract scraper base + ProductInfo + price/currency parsing helpers."""

from __future__ import annotations

import logging
import random
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, ClassVar
from urllib.parse import urlparse

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)


# ── User-agent pool (rotate per request) ─────────────────────────

# User-Agent strings are intentionally not wrapped (must match real browser headers verbatim).
USER_AGENTS: list[str] = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:133.0) Gecko/20100101 Firefox/133.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.1 Safari/605.1.15",  # noqa: E501
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36 Edg/131.0.0.0",  # noqa: E501
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",  # noqa: E501
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",  # noqa: E501
]


def get_headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Return browser-like headers with a random User-Agent."""
    headers: dict[str, str] = {
        "User-Agent": random.choice(USER_AGENTS),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",  # noqa: E501
        "Accept-Language": "en-US,en;q=0.9,it-IT;q=0.8,it;q=0.7",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT": "1",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest": "document",
        "Sec-Fetch-Mode": "navigate",
        "Sec-Fetch-Site": "none",
        "Sec-Fetch-User": "?1",
        "Cache-Control": "max-age=0",
    }
    if extra:
        headers.update(extra)
    return headers


# ── Price parsing (returns Decimal, never float) ──────────────────

_CURRENCY_LABEL_RE = re.compile(r"(EUR|USD|GBP|CHF|JPY|SEK|NOK|DKK|PLN|CZK)", re.IGNORECASE)
_NUMERIC_NOISE_RE = re.compile(r"[€$£¥₹\s ​]")


def parse_price(price_str: str | None) -> Decimal | None:
    """Parse a price string in arbitrary international format into Decimal.

    Handles european comma decimal, US comma thousands, swiss apostrophe,
    multi-comma european thousands, and currency labels mixed in.
    """
    if not price_str:
        return None

    cleaned = _NUMERIC_NOISE_RE.sub("", price_str)
    cleaned = _CURRENCY_LABEL_RE.sub("", cleaned).strip()
    if not cleaned:
        return None

    # Swiss apostrophe = thousands sep
    if "'" in cleaned:
        cleaned = cleaned.replace("'", "")

    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")

    if last_dot == -1 and last_comma == -1:
        # Integer
        try:
            return Decimal(cleaned)
        except (InvalidOperation, ValueError):
            return None

    if last_dot > last_comma:
        # US-style: dot is decimal, comma is thousands
        cleaned = cleaned.replace(",", "")
    elif last_comma > last_dot:
        # Comma after dot (or no dot): could be euro decimal or US thousands
        if last_dot == -1 and cleaned.count(",") == 1:
            # Single comma, no dot — disambiguate by digits after the comma:
            #   3 digits after → US thousands (e.g. "$1,234")
            #   1–2 digits after → euro decimal (e.g. "29,99")
            #   else → treat as decimal
            after = cleaned[last_comma + 1 :]
            if len(after) == 3 and after.isdigit():
                cleaned = cleaned.replace(",", "")
            else:
                cleaned = cleaned.replace(",", ".")
        else:
            # Euro-style with thousands: comma is decimal, dot is thousands
            cleaned = cleaned.replace(".", "")
            # Replace ONLY the rightmost comma with a decimal point; drop earlier ones
            last = cleaned.rfind(",")
            cleaned = cleaned[:last].replace(",", "") + "." + cleaned[last + 1 :]

    try:
        return Decimal(cleaned)
    except (InvalidOperation, ValueError):
        return None


# ── Currency detection ───────────────────────────────────────────

_CURRENCY_SIGNS: list[tuple[str, str]] = [
    # Order matters: longer matches first (CHF before generic letter triggers)
    ("CHF", "CHF"),
    ("EUR", "EUR"),
    ("USD", "USD"),
    ("GBP", "GBP"),
    ("JPY", "JPY"),
    ("SEK", "SEK"),
    ("NOK", "NOK"),
    ("DKK", "DKK"),
    ("PLN", "PLN"),
    ("CZK", "CZK"),
    ("€", "EUR"),
    ("$", "USD"),
    ("£", "GBP"),
    ("¥", "JPY"),
    ("zł", "PLN"),
    ("kr", "SEK"),  # 'kr' last — overlaps with NOK/DKK; ambiguous, default SEK
]


def detect_currency(text: str | None) -> str | None:
    """Detect ISO-4217 currency code from a string. Returns None if unknown."""
    if not text:
        return None
    upper = text.upper()
    for sign, code in _CURRENCY_SIGNS:
        if sign.upper() in upper:
            return code
    return None


# ── ProductInfo & AbstractScraper ────────────────────────────────


@dataclass
class ProductInfo:
    """Result of a scrape attempt. All fields optional; `error` set on failure."""

    name: str | None = None
    price: Decimal | None = None
    currency: str | None = None
    available: bool = True
    seller: str | None = None
    condition: str | None = None
    error: str | None = None


class AbstractScraper(ABC):
    """Base class for site-specific scrapers."""

    name: ClassVar[str] = ""
    priority: ClassVar[int] = 0
    domain_patterns: ClassVar[list[re.Pattern[str]]] = []

    @abstractmethod
    def can_handle(self, url: str) -> bool:
        """Return True if this scraper should attempt the URL."""

    @abstractmethod
    async def scrape(self, url: str, client: httpx.AsyncClient) -> ProductInfo:
        """Fetch and parse the product page. Always return a ProductInfo (never raise)."""

    def matches_domain(self, url: str) -> bool:
        """Helper for `can_handle`: True if URL netloc matches any domain_patterns."""
        try:
            netloc = urlparse(url).netloc.lower()
        except (ValueError, TypeError):
            return False
        return any(p.search(netloc) for p in self.domain_patterns)
