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

    from price_tracker.core.health import HealthManager

from price_tracker.core.exceptions import BlockEvent, CaptchaDetected, HTTPBlockStatus, WAFBlocked

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

# Trailing German "no cents" marker: "349,-" / "1.299,-" → keep the separator, drop the dash.
_NO_CENTS_RE = re.compile(r"([.,])\s*-\s*$")
# Anything that is NOT a digit or a thousands/decimal separator. Strips currency symbols
# and labels (€, $, zł, Kč, kr, EUR, ...), spaces, thin/zero-width spaces and stray chars.
_NON_NUMERIC_RE = re.compile(r"[^\d.,']")


def _to_decimal(s: str) -> Decimal | None:
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _is_thousands_grouping(groups: list[str]) -> bool:
    """True if `groups` form a valid thousands grouping: first 1–3 digits, the rest exactly 3."""
    if len(groups) < 2 or not groups[0] or len(groups[0]) > 3 or not groups[0].isdigit():
        return False
    return all(len(g) == 3 and g.isdigit() for g in groups[1:])


def parse_price(price_str: str | None) -> Decimal | None:
    """Parse a price string in arbitrary international format into Decimal.

    Handles EU comma-decimal, US comma-thousands, Swiss apostrophe, EU/US thousands
    with NO decimal part, German "349,-" notation, and currency labels/symbols mixed
    in (€, $, zł, Kč, kr, ISO codes).

    Disambiguation: a lone separator followed by exactly three digits (e.g. "1.299"
    or "1,234") is read as thousands grouping, NOT a three-decimal fraction — retail
    prices never carry three decimals, while EU dot-thousands integers ("1.299 €" =
    1299) are extremely common. (Trade-off: 3-decimal niche prices like fuel/crypto
    are out of scope for this e-commerce tracker.)
    """
    if not price_str:
        return None

    # "349,-" → "349," so the integer survives the rest of the pipeline.
    s = _NO_CENTS_RE.sub(r"\1", price_str)
    cleaned = _NON_NUMERIC_RE.sub("", s)
    cleaned = cleaned.replace("'", "")  # Swiss apostrophe is always a thousands sep.
    if not cleaned:
        return None

    has_dot = "." in cleaned
    has_comma = "," in cleaned

    # No separators → plain integer.
    if not has_dot and not has_comma:
        return _to_decimal(cleaned)

    # Both separators present → whichever appears LAST is the decimal point.
    if has_dot and has_comma:
        if cleaned.rfind(".") > cleaned.rfind(","):
            # US: dot decimal, comma thousands.
            return _to_decimal(cleaned.replace(",", ""))
        # EU: comma decimal, dot thousands.
        last = cleaned.rfind(",")
        whole = cleaned[:last].replace(".", "").replace(",", "")
        return _to_decimal(whole + "." + cleaned[last + 1 :])

    # Exactly one kind of separator present.
    sep = "." if has_dot else ","
    if cleaned.count(sep) == 1:
        before, after = cleaned.split(sep)
        # >3 trailing digits is neither a 2-decimal fraction nor a 3-digit thousands
        # group — typically a split-span concatenation ("$1,299"+"99" → "1,29999").
        # Reject so the scraper can fall back to a reliable source (bug #12).
        if len(after) > 3:
            return None
        # Lone separator + exactly 3 trailing digits + non-zero leading group → thousands.
        if len(after) == 3 and after.isdigit() and before[:1] not in ("", "0"):
            return _to_decimal(before + after)
        return _to_decimal(before + "." + after)

    # Multiple separators of the same kind.
    groups = cleaned.split(sep)
    if sep == ",":
        # US thousands only if every group lines up ("1,234,567"); otherwise the last
        # comma is an EU decimal ("5,250,00" → 5250.00).
        if _is_thousands_grouping(groups):
            return _to_decimal(cleaned.replace(",", ""))
        last = cleaned.rfind(",")
        return _to_decimal(cleaned[:last].replace(",", "") + "." + cleaned[last + 1 :])
    # Multiple dots: only valid as EU thousands grouping, else malformed ("1.2.3.4").
    if _is_thousands_grouping(groups):
        return _to_decimal(cleaned.replace(".", ""))
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


# ── JSON-LD offer selection (shared across scrapers) ─────────────

_FINANCING_RE = re.compile(
    r"/mo\b|/month|per month|a month|al mese|/mese|monthly|installment|financ|rate ", re.IGNORECASE
)


def _is_financing_offer(offer: dict[str, object]) -> bool:
    """True when an Offer represents a recurring/monthly financing entry, not the price."""
    spec = offer.get("priceSpecification")
    specs = spec if isinstance(spec, list) else [spec]
    for s in specs:
        if isinstance(s, dict) and (
            "UnitPrice" in str(s.get("@type", ""))
            or s.get("billingDuration")
            or s.get("billingIncrement")
            or s.get("referenceQuantity")
        ):
            return True
    blob = " ".join(str(offer.get(k, "")) for k in ("name", "description", "category"))
    return bool(_FINANCING_RE.search(blob))


def select_jsonld_offer(offers: object) -> tuple[Decimal, str | None] | None:
    """Pick the representative (price, ISO-currency) from a JSON-LD ``offers`` value.

    Robust against the common precision traps: takes neither ``offers[0]`` blindly
    (cheapest variant / used / marketplace entry) nor an ``AggregateOffer``'s
    ``lowPrice`` ("from" price). Filters recurring/financing offers, then returns
    the HIGHEST remaining single ``price`` — the main buy price. The currency is
    normalised to ISO-4217 (a bare symbol like ``€`` becomes ``EUR``; an unknown
    value becomes ``None`` rather than leaking verbatim).
    """
    if isinstance(offers, dict):
        offer_list: list[dict[str, object]] = [offers]
    elif isinstance(offers, list):
        offer_list = [o for o in offers if isinstance(o, dict)]
    else:
        return None

    best: tuple[Decimal, str | None] | None = None
    for offer in offer_list:
        if _is_financing_offer(offer):
            continue
        # Only a concrete single `price`; never AggregateOffer low/highPrice.
        price_raw = offer.get("price")
        if price_raw is None:
            continue
        parsed = parse_price(str(price_raw))
        if parsed is None:
            continue
        raw_currency = offer.get("priceCurrency")
        currency = detect_currency(str(raw_currency)) if raw_currency else None
        if best is None or parsed > best[0]:
            best = (parsed, currency)
    return best


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


# ── Block detection ──────────────────────────────────────────────

_WAF_FINGERPRINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("cloudflare", re.compile(r"Just a moment\.\.\.|<title>Attention Required", re.IGNORECASE)),
    ("akamai", re.compile(r"\bAccess Denied\b", re.IGNORECASE)),
    ("imperva", re.compile(r"Incapsula incident ID", re.IGNORECASE)),
)

_CAPTCHA_FINGERPRINTS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("g-recaptcha", re.compile(r'class\s*=\s*["\']g-recaptcha', re.IGNORECASE)),
    ("hcaptcha", re.compile(r'class\s*=\s*["\']h-captcha', re.IGNORECASE)),
    ("captcha-form", re.compile(r'id\s*=\s*["\']captcha[\w-]*', re.IGNORECASE)),
)


def detect_block_event(*, status_code: int, body: str, url: str) -> None:
    """Inspect HTTP response for block markers; raise BlockEvent subclass if blocked.

    Block triggers (raise BlockEvent):
      - HTTP 429 (Too Many Requests) or 403 (Forbidden)
      - WAF challenge page (Cloudflare/Akamai/Imperva)
      - CAPTCHA challenge in body

    Non-block (returns None):
      - 2xx/3xx with normal product HTML
      - 4xx other than 403/429 (likely client error)
      - 5xx (server problem)
      - Network timeouts (raised by httpx, never reach here)

    Caller (scraper) must invoke this AFTER httpx call and BEFORE parsing.
    """
    if status_code in (403, 429):
        raise HTTPBlockStatus(status=status_code, url=url)

    for provider, pattern in _WAF_FINGERPRINTS:
        if pattern.search(body):
            raise WAFBlocked(provider=provider, url=url)

    for marker, pattern in _CAPTCHA_FINGERPRINTS:
        if pattern.search(body):
            raise CaptchaDetected(marker=marker, url=url)

    return None


# ── Pipeline helpers (HealthManager integration) ─────────────────


def _block_reason(exc: BlockEvent) -> str:
    if isinstance(exc, HTTPBlockStatus):
        return f"HTTP {exc.status}"
    if isinstance(exc, WAFBlocked):
        return f"WAF/{exc.provider}"
    if isinstance(exc, CaptchaDetected):
        return f"CAPTCHA/{exc.marker}"
    return "BlockEvent"


async def handle_block_in_pipeline(
    exc: BlockEvent,
    *,
    health_mgr: HealthManager,
    domain: str,
) -> None:
    """Record block event in HealthManager. Caller re-raises the exception."""
    await health_mgr.record_block(domain, reason=_block_reason(exc))


async def handle_success_in_pipeline(
    *,
    health_mgr: HealthManager,
    domain: str,
) -> None:
    """Record successful scrape in HealthManager."""
    await health_mgr.record_success(domain)
