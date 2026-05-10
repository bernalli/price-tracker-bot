"""Unit tests for GoogleStoreScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers.google_store import GoogleStoreScraper

if TYPE_CHECKING:
    from collections.abc import Callable


def test_google_store_handles_store_google_com() -> None:
    scraper = GoogleStoreScraper()
    assert scraper.can_handle("https://store.google.com/product/pixel_8")


def test_google_store_rejects_unrelated() -> None:
    scraper = GoogleStoreScraper()
    assert not scraper.can_handle("https://www.google.com/search?q=pixel")


def test_google_store_priority_50() -> None:
    assert GoogleStoreScraper.priority == 50


@pytest.mark.asyncio
async def test_google_store_extracts_price_from_fixture(
    load_fixture: Callable[[str], str],
) -> None:
    html = load_fixture("google_store/sample_product.html")
    scraper = GoogleStoreScraper()
    url = "https://store.google.com/product/pixel_8"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is not None
    assert info.price > Decimal("0")
    assert info.currency == "USD"
    assert info.name


@pytest.mark.asyncio
async def test_google_store_returns_error_on_missing_data() -> None:
    scraper = GoogleStoreScraper()
    url = "https://store.google.com/product/missing_xyz"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text="<html><body>nothing</body></html>")
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
