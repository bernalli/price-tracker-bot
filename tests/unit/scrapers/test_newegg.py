"""Unit tests for NeweggScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.newegg import NeweggScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_newegg_handles_newegg_domain() -> None:
    scraper = NeweggScraper()
    assert scraper.can_handle("https://www.newegg.com/p/N82E16819113841")


def test_newegg_rejects_unrelated() -> None:
    scraper = NeweggScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_newegg_priority_50() -> None:
    assert NeweggScraper.priority == 50


@pytest.mark.asyncio
async def test_newegg_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("newegg/sample_product.html")
    scraper = NeweggScraper()
    url = "https://www.newegg.com/p/N82E16819113841"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("USD", "EUR", "GBP", "CAD")
    assert info.name


@pytest.mark.asyncio
async def test_newegg_returns_error_on_missing_data() -> None:
    scraper = NeweggScraper()
    url = "https://www.newegg.com/p/N82E0000000"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
