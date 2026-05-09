"""Unit tests for AbstractScraper, ProductInfo, parse_price, detect_currency."""

from __future__ import annotations

from decimal import Decimal

import pytest

from price_tracker.core.scraper_base import (
    AbstractScraper,
    ProductInfo,
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
