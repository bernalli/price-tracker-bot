"""Unit tests for AliexpressScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.aliexpress import AliexpressScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_aliexpress_handles_com_domain() -> None:
    scraper = AliexpressScraper()
    assert scraper.can_handle("https://www.aliexpress.com/item/1005001234567890.html")


def test_aliexpress_handles_it_domain() -> None:
    scraper = AliexpressScraper()
    assert scraper.can_handle("https://www.aliexpress.it/item/1005001234567890.html")


def test_aliexpress_handles_ru_domain() -> None:
    scraper = AliexpressScraper()
    assert scraper.can_handle("https://www.aliexpress.ru/item/1005001234567890.html")


def test_aliexpress_rejects_unrelated() -> None:
    scraper = AliexpressScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_aliexpress_priority_50() -> None:
    assert AliexpressScraper.priority == 50


@pytest.mark.asyncio
async def test_aliexpress_extracts_price_from_runparams(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("aliexpress/sample_product.html")
    scraper = AliexpressScraper()
    url = "https://www.aliexpress.com/item/1005001234567890.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price == Decimal("12.99")
    assert info.currency == "USD"
    assert info.name


@pytest.mark.asyncio
async def test_aliexpress_falls_back_to_dom(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("aliexpress/sample_dom_only.html")
    scraper = AliexpressScraper()
    url = "https://www.aliexpress.com/item/1005009999999999.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price == Decimal("9.50")
    assert info.currency == "EUR"


@pytest.mark.asyncio
async def test_aliexpress_returns_error_on_missing_data() -> None:
    scraper = AliexpressScraper()
    url = "https://www.aliexpress.com/item/0.html"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
