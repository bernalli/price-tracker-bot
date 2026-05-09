"""Smoke test for AmazonScraper."""

from __future__ import annotations

from price_tracker.scrapers.amazon import AmazonScraper


def test_amazon_handles_amazon_urls():
    s = AmazonScraper()
    assert s.can_handle("https://www.amazon.it/dp/B01")
    assert s.can_handle("https://www.amazon.com/gp/product/B02")
    assert s.can_handle("https://www.amazon.de/dp/B03")
    assert not s.can_handle("https://www.ebay.com/itm/123")


def test_amazon_priority_high():
    assert AmazonScraper.priority == 100
