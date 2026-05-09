"""Unit tests for AbstractScraper, ProductInfo, parse_price, detect_currency."""

from __future__ import annotations

from decimal import Decimal

import pytest

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
    parse_price,
)


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


def test_parse_price_returns_none_on_garbage():
    assert parse_price("") is None
    assert parse_price("not a price") is None
    assert parse_price(None) is None  # type: ignore[arg-type]


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
        domain_patterns: list = []

    with pytest.raises(TypeError):
        BadScraper()  # missing can_handle and scrape

    class GoodScraper(AbstractScraper):
        name = "good"
        priority = 0
        domain_patterns: list = []

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
        assert detect_block_event(status_code=200, body="<html>ok</html>", url="x") is None

    def test_status_500_no_raise(self):
        # 5xx is server error, not block
        assert detect_block_event(status_code=502, body="", url="x") is None

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
