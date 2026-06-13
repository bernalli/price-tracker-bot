"""Unit tests for AbstractScraper, ProductInfo, parse_price, detect_currency."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import pytest
from bs4 import BeautifulSoup

from price_tracker.core.exceptions import (
    CaptchaDetected,
    HTTPBlockStatus,
    WAFBlocked,
)
from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
    detect_block_event,
    detect_currency,
    find_microdata_price_el,
    parse_price,
    select_jsonld_offer,
)

if TYPE_CHECKING:
    import re


def test_product_info_defaults():
    info = ProductInfo()
    assert info.name is None
    assert info.price is None
    assert info.currency is None
    assert info.error is None


def test_product_info_with_values():
    info = ProductInfo(name="Widget", price=Decimal("12.34"), currency="EUR")
    assert info.name == "Widget"
    assert info.price == Decimal("12.34")
    assert info.currency == "EUR"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("29,99 €", Decimal("29.99")),
        ("€29.99", Decimal("29.99")),
        ("1.299,99", Decimal("1299.99")),
        ("1,299.99", Decimal("1299.99")),
        ("5'250.00", Decimal("5250.00")),
        ("CHF 5,250,00", Decimal("5250.00")),
        ("$1,234", Decimal("1234")),
        ("EUR 29,99", Decimal("29.99")),
    ],
)
def test_parse_price_returns_decimal(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # EU dot-thousands with NO decimal part (was parsed 1000x too low) — bug #3
        ("1.299", Decimal("1299")),
        ("2.499", Decimal("2499")),
        ("12.999", Decimal("12999")),
        ("1.234", Decimal("1234")),
        ("1.299 €", Decimal("1299")),
        # EU multi-dot integer thousands (was returning None) — bug #22
        ("1.234.567", Decimal("1234567")),
        ("12.345.678", Decimal("12345678")),
        # US multi-comma integer thousands (was mis-parsed to 1234.567) — bug #23
        ("1,234,567", Decimal("1234567")),
        ("$12,345,678", Decimal("12345678")),
        # German "no cents" notation 349,- / 1.299,- — bug #44
        ("349,-", Decimal("349")),
        ("799,-", Decimal("799")),
        ("1.299,-", Decimal("1299")),
        # Non-ASCII currency labels not stripped before → no price — bug #50
        ("1 299,00 zł", Decimal("1299.00")),
        ("129,00 zł", Decimal("129.00")),
        ("1 299 Kč", Decimal("1299")),
        ("1 299,00 kr", Decimal("1299.00")),
        # Leading-zero decimals must stay decimals, NOT become thousands
        ("0.999", Decimal("0.999")),
        ("0,99", Decimal("0.99")),
        # Space/thin-space thousands (French/Polish) with decimal
        ("1 234,56", Decimal("1234.56")),
    ],
)
def test_parse_price_international_formats(raw, expected):
    assert parse_price(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # Split-span React concatenation "$1,299" + "99" → "$1,29999": a single
        # separator with >3 trailing digits is neither a 2-decimal nor a 3-digit
        # thousands group → reject so the scraper falls back to JSON-LD (bug #12).
        "$1,29999",
        "1,29999",
        "1.29999",
        "1234,5678",
    ],
)
def test_parse_price_rejects_split_span_concatenation(raw):
    assert parse_price(raw) is None


def test_select_jsonld_offer_skips_financing_and_takes_highest():
    offers = [
        {"price": "1299.00", "priceCurrency": "USD"},
        {"price": "54.08", "priceCurrency": "USD", "name": "$54.08/mo. financing"},
        {"price": "1199.00", "priceCurrency": "USD"},
    ]
    result = select_jsonld_offer(offers)
    assert result == (Decimal("1299.00"), "USD")


def test_select_jsonld_offer_filters_unit_price_specification():
    offers = [
        {
            "price": "50.00",
            "priceCurrency": "EUR",
            "priceSpecification": {"@type": "UnitPriceSpecification", "billingDuration": 24},
        }
    ]
    assert select_jsonld_offer(offers) is None


def test_select_jsonld_offer_ignores_aggregate_low_price():
    # AggregateOffer with no concrete `price` → must NOT leak the 'from' lowPrice.
    offers = {"@type": "AggregateOffer", "lowPrice": "999", "highPrice": "1299"}
    assert select_jsonld_offer(offers) is None


def test_select_jsonld_offer_uses_aggregate_price_when_present():
    offers = {"@type": "AggregateOffer", "price": "1099", "lowPrice": "999"}
    assert select_jsonld_offer(offers) == (Decimal("1099"), None)


def test_select_jsonld_offer_normalizes_symbol_currency():
    offers = {"price": "29,99", "priceCurrency": "€"}
    assert select_jsonld_offer(offers) == (Decimal("29.99"), "EUR")


def test_select_jsonld_offer_handles_empty_and_invalid():
    assert select_jsonld_offer([]) is None
    assert select_jsonld_offer(None) is None
    assert select_jsonld_offer("nope") is None


def test_find_microdata_price_el_prefers_offer_scope():
    html = (
        '<div class="recs"><span itemprop="price" content="9.99">9.99</span></div>'
        '<div itemprop="offers"><span itemprop="price" content="199.99">199.99</span></div>'
    )
    el = find_microdata_price_el(BeautifulSoup(html, "lxml"))
    assert el is not None
    assert el.get("content") == "199.99"


def test_find_microdata_price_el_falls_back_to_first():
    el = find_microdata_price_el(
        BeautifulSoup('<span itemprop="price" content="50.00">50.00</span>', "lxml")
    )
    assert el is not None
    assert el.get("content") == "50.00"


def test_find_microdata_price_el_skips_carousel_scope():
    """A complete Product/Offer itemscope inside a carousel container must not win (#37)."""
    html = (
        '<div class="related-carousel">'
        '<div itemscope itemtype="https://schema.org/Product">'
        '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
        '<span itemprop="price" content="9.99">9.99</span>'
        "</div></div></div>"
        '<div itemscope itemtype="https://schema.org/Product">'
        '<div itemprop="offers" itemscope itemtype="https://schema.org/Offer">'
        '<span itemprop="price" content="499.00">499.00</span>'
        "</div></div>"
    )
    el = find_microdata_price_el(BeautifulSoup(html, "lxml"))
    assert el is not None
    assert el.get("content") == "499.00"


def test_find_microdata_price_el_keeps_first_when_all_scopes_in_carousel():
    """If every scope sits in a carousel container, keep the first-match behavior (#37)."""
    html = (
        '<div class="recommendations-carousel">'
        '<div itemprop="offers"><span itemprop="price" content="9.99">9.99</span></div>'
        "</div>"
    )
    el = find_microdata_price_el(BeautifulSoup(html, "lxml"))
    assert el is not None
    assert el.get("content") == "9.99"


def test_parse_price_returns_none_on_garbage():
    assert parse_price("") is None
    assert parse_price("not a price") is None
    assert parse_price(None) is None


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("29,99 €", "EUR"),
        ("$29.99", "USD"),
        ("£29.99", "GBP"),
        ("¥1000", "JPY"),
        ("CHF 25.00", "CHF"),
    ],
)
def test_detect_currency(text, expected):
    assert detect_currency(text) == expected


def test_abstract_scraper_requires_can_handle_and_scrape():
    class BadScraper(AbstractScraper):
        name = "bad"
        priority = 0
        # Test subclass: ClassVar override with empty list to verify ABC enforcement.
        domain_patterns: list[re.Pattern[str]] = []  # type: ignore[misc]

    with pytest.raises(TypeError):
        BadScraper()  # type: ignore[abstract]  # missing can_handle and scrape

    class GoodScraper(AbstractScraper):
        name = "good"
        priority = 0
        domain_patterns: list[re.Pattern[str]] = []  # type: ignore[misc]

        def can_handle(self, url: str) -> bool:
            return True

        async def scrape(self, url, client):
            return ProductInfo(price=Decimal("1"))

    GoodScraper()  # no error


def test_parse_price_returns_none_when_only_currency_label():
    """Input that contains ONLY currency text strips to empty after sub → None."""
    assert parse_price("EUR") is None
    assert parse_price("CHF ") is None


def test_parse_price_returns_none_on_invalid_decimal_format():
    """A cleaned string that fails Decimal() conversion → None."""
    # Multiple dots without thousands grouping → InvalidOperation
    assert parse_price("1.2.3.4") is None


def test_matches_domain_returns_false_for_empty_url():
    """matches_domain on URL with empty netloc returns False (no pattern match)."""
    import re

    class _Scraper(AbstractScraper):
        name = "x"
        priority = 0
        domain_patterns = [re.compile(r"example\.")]

        def can_handle(self, url: str) -> bool:
            return self.matches_domain(url)

        async def scrape(self, url, client):
            return ProductInfo()

    s = _Scraper()
    assert s.matches_domain("") is False
    assert s.matches_domain("not a url at all") is False


class TestDetectBlockEvent:
    def test_status_429_raises(self):
        with pytest.raises(HTTPBlockStatus) as exc_info:
            detect_block_event(status_code=429, body="", url="https://x.com")
        assert exc_info.value.status == 429

    def test_status_403_raises(self):
        with pytest.raises(HTTPBlockStatus):
            detect_block_event(status_code=403, body="", url="https://x.com")

    def test_status_200_no_raise(self):
        # No raise — function returns None on no block detected
        detect_block_event(status_code=200, body="<html>ok</html>", url="x")

    def test_status_500_no_raise(self):
        # 5xx is server error, not block
        detect_block_event(status_code=502, body="", url="x")

    def test_cloudflare_marker(self):
        body = "<html><head><title>Just a moment...</title></head><body>...</body></html>"
        with pytest.raises(WAFBlocked) as exc_info:
            detect_block_event(status_code=200, body=body, url="https://x.com")
        assert exc_info.value.provider == "cloudflare"

    def test_akamai_marker(self):
        body = "<html><body>Access Denied</body></html>"
        with pytest.raises(WAFBlocked) as exc_info:
            detect_block_event(status_code=200, body=body, url="x")
        assert exc_info.value.provider == "akamai"

    def test_imperva_marker(self):
        body = "Request unsuccessful. Incapsula incident ID: 123-456"
        with pytest.raises(WAFBlocked) as exc_info:
            detect_block_event(status_code=200, body=body, url="x")
        assert exc_info.value.provider == "imperva"

    def test_recaptcha_marker(self):
        body = '<div class="g-recaptcha" data-sitekey="..."></div>'
        with pytest.raises(CaptchaDetected) as exc_info:
            detect_block_event(status_code=200, body=body, url="x")
        assert exc_info.value.marker == "g-recaptcha"

    def test_generic_captcha_marker(self):
        body = '<form id="captcha-form">...</form>'
        with pytest.raises(CaptchaDetected):
            detect_block_event(status_code=200, body=body, url="x")


# ── Pipeline helper tests ─────────────────────────────────────────


from unittest.mock import AsyncMock  # noqa: E402


@pytest.mark.asyncio
async def test_scraper_base_invokes_health_on_block_event():
    from price_tracker.core.exceptions import HTTPBlockStatus
    from price_tracker.core.scraper_base import handle_block_in_pipeline

    health_mgr = AsyncMock()
    exc = HTTPBlockStatus(status=429, url="https://xteink.com/p/1")
    await handle_block_in_pipeline(exc, health_mgr=health_mgr, domain="xteink.com")
    health_mgr.record_block.assert_awaited_once_with("xteink.com", reason="HTTP 429")


@pytest.mark.asyncio
async def test_scraper_base_invokes_health_on_success():
    from price_tracker.core.scraper_base import handle_success_in_pipeline

    health_mgr = AsyncMock()
    await handle_success_in_pipeline(health_mgr=health_mgr, domain="amazon.com")
    health_mgr.record_success.assert_awaited_once_with("amazon.com")


def test_brotli_available_for_httpx_decompression() -> None:
    """`get_headers()` advertises ``Accept-Encoding: gzip, deflate, br`` and
    httpx silently delivers garbage when a server responds with brotli and
    neither ``brotli`` nor ``brotlicffi`` is installed.

    Regression: nove25.net serves brotli by default; without this package,
    its 436 KB JSON-LD+OG page came back as a 55 KB skeleton, so the
    Nove25 scraper added in v0.1.10 silently returned ``price=None`` in
    production despite passing every offline test.
    """
    import importlib.util

    if (
        importlib.util.find_spec("brotli") is None
        and importlib.util.find_spec("brotlicffi") is None
    ):
        pytest.fail(
            "brotli/brotlicffi not installed — httpx cannot decode "
            "br-encoded responses (which get_headers() advertises)"
        )
