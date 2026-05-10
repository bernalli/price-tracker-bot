"""Unit tests for MediamarktScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.mediamarkt import MediamarktScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_mediamarkt_handles_de_domain() -> None:
    scraper = MediamarktScraper()
    assert scraper.can_handle("https://www.mediamarkt.de/de/product/_sample-1234.html")


def test_mediamarkt_handles_it_domain() -> None:
    scraper = MediamarktScraper()
    assert scraper.can_handle("https://www.mediamarkt.it/it/product/_sample-1234.html")


def test_mediamarkt_handles_ch_domain() -> None:
    scraper = MediamarktScraper()
    assert scraper.can_handle("https://www.mediamarkt.ch/de/product/_sample-1234.html")


def test_mediamarkt_rejects_unrelated() -> None:
    scraper = MediamarktScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_mediamarkt_priority_50() -> None:
    assert MediamarktScraper.priority == 50


@pytest.mark.asyncio
async def test_mediamarkt_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("mediamarkt/sample_product.html")
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.de/de/product/_sample-1234.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency in ("EUR", "USD", "CHF", "PLN", "HUF")
    assert info.name


@pytest.mark.asyncio
async def test_mediamarkt_default_currency_ch_is_chf() -> None:
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.ch/de/product/_sample-9999.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body><h1>x</h1></body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.currency == "CHF"
    assert info.error is not None


@pytest.mark.asyncio
async def test_mediamarkt_returns_error_on_missing_data() -> None:
    scraper = MediamarktScraper()
    url = "https://www.mediamarkt.de/de/product/_missing-9999.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
