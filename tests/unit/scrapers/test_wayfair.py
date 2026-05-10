"""Unit tests for WayfairScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.wayfair import WayfairScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_wayfair_handles_wayfair_com() -> None:
    scraper = WayfairScraper()
    assert scraper.can_handle("https://www.wayfair.com/furniture/pdp/sample-w0001.html")


def test_wayfair_handles_wayfair_uk() -> None:
    scraper = WayfairScraper()
    assert scraper.can_handle("https://www.wayfair.co.uk/furniture/pdp/sample-w0001.html")


def test_wayfair_handles_wayfair_de() -> None:
    scraper = WayfairScraper()
    assert scraper.can_handle("https://www.wayfair.de/moebel/pdp/sample-w0001.html")


def test_wayfair_rejects_unrelated() -> None:
    scraper = WayfairScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_wayfair_priority_50() -> None:
    assert WayfairScraper.priority == 50


@pytest.mark.asyncio
async def test_wayfair_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("wayfair/sample_product.html")
    scraper = WayfairScraper()
    url = "https://www.wayfair.com/furniture/pdp/sample-w0001.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("USD", "EUR", "GBP", "CAD")
    assert info.name


@pytest.mark.asyncio
async def test_wayfair_returns_error_on_missing_data() -> None:
    scraper = WayfairScraper()
    url = "https://www.wayfair.com/furniture/pdp/missing-w9999.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
