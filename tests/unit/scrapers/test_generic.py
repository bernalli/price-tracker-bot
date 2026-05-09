"""Smoke test for GenericScraper."""

from __future__ import annotations

from price_tracker.scrapers.generic import GenericScraper


def test_generic_handles_anything():
    s = GenericScraper()
    assert s.can_handle("https://random-site.example.com/product")
    assert s.can_handle("https://www.somestore.io/p/123")


def test_generic_priority_zero():
    assert GenericScraper.priority == 0
