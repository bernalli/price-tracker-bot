"""Smoke test for EbayScraper."""

from __future__ import annotations

from price_tracker.scrapers.ebay import EbayScraper


def test_ebay_handles_ebay_urls():
    s = EbayScraper()
    assert s.can_handle("https://www.ebay.com/itm/123")
    assert s.can_handle("https://www.ebay.it/itm/456")
    assert not s.can_handle("https://www.amazon.com/dp/B01")


def test_ebay_priority():
    assert EbayScraper.priority == 90
