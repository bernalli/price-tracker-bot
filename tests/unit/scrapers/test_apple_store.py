"""Unit tests for AppleStoreScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.apple_store import AppleStoreScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_apple_store_handles_shop_path() -> None:
    scraper = AppleStoreScraper()
    assert scraper.can_handle("https://www.apple.com/shop/buy-iphone/iphone-15")


def test_apple_store_handles_locale_shop_path() -> None:
    scraper = AppleStoreScraper()
    assert scraper.can_handle("https://www.apple.com/uk/shop/buy-iphone/iphone-15")


def test_apple_store_rejects_non_shop_path() -> None:
    scraper = AppleStoreScraper()
    assert not scraper.can_handle("https://www.apple.com/iphone-15/")


def test_apple_store_rejects_unrelated() -> None:
    scraper = AppleStoreScraper()
    assert not scraper.can_handle("https://www.amazon.com/dp/B01")


def test_apple_store_priority_50() -> None:
    assert AppleStoreScraper.priority == 50


@pytest.mark.asyncio
async def test_apple_store_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("apple_store/sample_product.html")
    scraper = AppleStoreScraper()
    url = "https://www.apple.com/shop/buy-iphone/iphone-15"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency == "USD"
    assert info.name


@pytest.mark.asyncio
async def test_apple_store_default_currency_uk_is_gbp() -> None:
    scraper = AppleStoreScraper()
    url = "https://www.apple.com/uk/shop/buy-iphone/missing-1"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body><h1>x</h1></body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.currency == "GBP"
    assert info.error is not None


@pytest.mark.asyncio
async def test_apple_store_returns_error_on_missing_data() -> None:
    scraper = AppleStoreScraper()
    url = "https://www.apple.com/shop/buy-iphone/missing-9999"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
