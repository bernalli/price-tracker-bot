"""Smoke tests for PlaywrightFallbackScraper.

E2E rendering is not covered — these tests cover only the static
metadata (priority, can_handle, available()) and the no-playwright code
path that returns a clean error.
"""

from __future__ import annotations

import pytest

from price_tracker.scrapers import playwright_fallback as pw_module
from price_tracker.scrapers.playwright_fallback import PlaywrightFallbackScraper


def test_playwright_priority_low():
    assert PlaywrightFallbackScraper.priority == 10


def test_playwright_can_handle_anything():
    scraper = PlaywrightFallbackScraper()
    assert scraper.can_handle("https://anything.example.com/")
    assert scraper.can_handle("https://shop.example.com/products/abc")
    assert scraper.can_handle("https://www.amazon.it/dp/B01")


def test_playwright_priority_below_specific_scrapers():
    """Registry ordering: specific scrapers (Amazon/eBay/Shopify) win first."""
    from price_tracker.scrapers.amazon import AmazonScraper
    from price_tracker.scrapers.ebay import EbayScraper
    from price_tracker.scrapers.shopify import ShopifyScraper

    assert PlaywrightFallbackScraper.priority < AmazonScraper.priority
    assert PlaywrightFallbackScraper.priority < EbayScraper.priority
    assert PlaywrightFallbackScraper.priority < ShopifyScraper.priority


@pytest.mark.asyncio
async def test_playwright_returns_error_when_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When playwright is not installed, scrape() returns a clean error (no crash)."""
    scraper = PlaywrightFallbackScraper()

    monkeypatch.setattr(pw_module, "available", lambda: False)

    info = await scraper.scrape("https://example.com/anything", client=None)

    assert info.price is None
    assert info.error is not None
    assert "Playwright" in info.error
