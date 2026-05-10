"""Unit tests for OttoScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.otto import OttoScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_otto_handles_otto_de() -> None:
    scraper = OttoScraper()
    assert scraper.can_handle("https://www.otto.de/p/sample-12345/")


def test_otto_rejects_non_otto() -> None:
    scraper = OttoScraper()
    assert not scraper.can_handle("https://www.amazon.de/dp/B01")


def test_otto_priority_50() -> None:
    assert OttoScraper.priority == 50


@pytest.mark.asyncio
async def test_otto_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("otto/sample_product.html")
    scraper = OttoScraper()
    url = "https://www.otto.de/p/sample-12345/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency == "EUR"
    assert info.name


@pytest.mark.asyncio
async def test_otto_returns_error_on_missing_data() -> None:
    scraper = OttoScraper()
    url = "https://www.otto.de/p/missing-9999/"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
