"""Unit tests for WalmartScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.walmart import WalmartScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_walmart_handles_walmart_domain() -> None:
    scraper = WalmartScraper()
    assert scraper.can_handle("https://www.walmart.com/ip/123")


def test_walmart_rejects_unrelated() -> None:
    scraper = WalmartScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_walmart_priority_50() -> None:
    assert WalmartScraper.priority == 50


@pytest.mark.asyncio
async def test_walmart_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("walmart/sample_product.html")
    scraper = WalmartScraper()
    url = "https://www.walmart.com/ip/123"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("USD", "EUR", "GBP", "CAD")
    assert info.name


@pytest.mark.asyncio
async def test_walmart_returns_error_on_missing_data() -> None:
    scraper = WalmartScraper()
    url = "https://www.walmart.com/ip/999"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
