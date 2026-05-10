"""Unit tests for GoogleStoreScraper."""

from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING

import httpx
import pytest
import respx

from price_tracker.scrapers import google_store as google_store_module
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


@pytest.mark.asyncio
async def test_google_store_dom_fallback_when_no_jsonld(
    load_fixture: Callable[[str], str],
) -> None:
    """Fixture with only DOM .O5JUAd (no JSON-LD) → DOM fallback yields price."""
    html = load_fixture("google_store/sample_google_store_fallback.html")
    scraper = GoogleStoreScraper()
    url = "https://store.google.com/product/pixel_fallback"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("499.00")
    assert info.currency == "USD"
    assert info.error is None


@pytest.mark.asyncio
async def test_google_store_returns_error_on_http_500(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """5xx response → ProductInfo with HTTP error."""
    scraper = GoogleStoreScraper()

    async def _fast_fetch(url: str, client: httpx.AsyncClient) -> httpx.Response:
        return await client.get(url)

    monkeypatch.setattr(google_store_module, "_fetch_google_store_html", _fast_fetch)

    url = "https://store.google.com/product/pixel_5555"
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(500)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price is None
    assert info.error is not None
    assert "HTTP error" in info.error


@pytest.mark.asyncio
async def test_google_store_dom_fallback_eur_symbol() -> None:
    """DOM fallback recognises € symbol and maps to EUR currency."""
    scraper = GoogleStoreScraper()
    url = "https://store.google.com/product/pixel_eur"
    html = """
    <html><head><title>Pixel EU</title></head><body>
    <h1>Pixel EU</h1>
    <span class="O5JUAd">€ 599,00</span>
    </body></html>
    """
    with respx.mock(assert_all_called=False) as router:
        router.get(url).respond(200, text=html)
        async with httpx.AsyncClient() as client:
            info = await scraper.scrape(url, client)
    assert info.price == Decimal("599.00")
    assert info.currency == "EUR"
