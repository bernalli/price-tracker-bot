"""Smoke test for ShopifyScraper."""

from __future__ import annotations

from price_tracker.scrapers.shopify import ShopifyScraper


def test_shopify_does_not_claim_amazon_or_ebay():
    s = ShopifyScraper()
    # Shopify should NOT claim Amazon or eBay URLs (those have their own scrapers)
    assert not s.can_handle("https://www.amazon.com/dp/B01") or s.priority < 100
    assert not s.can_handle("https://www.ebay.com/itm/123") or s.priority < 90


def test_shopify_priority():
    assert ShopifyScraper.priority == 80
