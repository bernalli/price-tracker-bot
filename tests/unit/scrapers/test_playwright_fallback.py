"""Smoke test for PlaywrightFallbackScraper."""

from __future__ import annotations

from price_tracker.scrapers.playwright_fallback import PlaywrightFallbackScraper


def test_playwright_priority_low():
    assert PlaywrightFallbackScraper.priority == 10
