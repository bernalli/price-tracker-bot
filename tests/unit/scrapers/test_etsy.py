"""Unit tests for EtsyScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.etsy import EtsyScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_etsy_handles_etsy_domain() -> None:
    scraper = EtsyScraper()
    assert scraper.can_handle("https://www.etsy.com/listing/123/sample")


def test_etsy_rejects_unrelated() -> None:
    scraper = EtsyScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_etsy_priority_50() -> None:
    assert EtsyScraper.priority == 50


@pytest.mark.asyncio
async def test_etsy_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("etsy/sample_product.html")
    scraper = EtsyScraper()
    url = "https://www.etsy.com/listing/123/sample"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("USD", "EUR", "GBP", "CAD")
    assert info.name


@pytest.mark.asyncio
async def test_etsy_returns_error_on_missing_data() -> None:
    scraper = EtsyScraper()
    url = "https://www.etsy.com/listing/999/missing"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
